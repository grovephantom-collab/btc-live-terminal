import time, threading, logging
from flask import Flask, request, jsonify
import config, database, market_data, smc_engine, trade_manager, notifications

log = logging.getLogger("Supervisor")
LOCK = threading.Lock()
STARTED = time.time()
STATE = {"status": "STARTING", "last_scan_epoch": 0, "last_signal": None, "last_error": None, "fencing_token": None}
STRUCTURE_CACHE = {}

def engine_supervision_loop():
    while True:
        try:
            is_leader, token = database.lease_tick()
            if not is_leader or token is None:
                STATE.update(status="STANDBY", fencing_token=None)
                time.sleep(2)
                continue

            STATE.update(status="RUNNING", fencing_token=token)
            if not market_data.refresh_all_market_data(LOCK):
                STATE.update(last_error="Market refresh issue")
                time.sleep(config.SCAN_SECONDS)
                continue

            trade_manager.manage_all_positions(token, LOCK)
            auto_enabled = database.get_setting("auto_trade_enabled", "1") == "1"

            for sym in config.SYMBOLS:
                with LOCK:
                    m = dict(market_data.get_market(sym))

                st = smc_engine.get_structure(m, sym)
                if st:
                    with LOCK:
                        STRUCTURE_CACHE[sym] = {
                            "macro_bias": st.get("macro_bias", "NEUTRAL"),
                            "adx": round(float(st.get("adx", 0.0)), 1),
                            "rsi": round(float(st.get("rsi", 50.0)), 1),
                            "cvd_ratio": round(float(st.get("cvd_ratio", 0.5)) * 100.0, 1),
                            "choch_status": st.get("choch_status", "NONE"),
                            "is_choppy": bool(st.get("is_choppy", False)),
                        }

                if not auto_enabled:
                    continue

                p = m.get("price")
                sess = m.get("session")
                poc = m.get("poc")
                k15 = m.get("klines", {}).get("15m", [])

                sig = smc_engine.evaluate_signal(st, k15, p, sess, poc, sym)
                if sig:
                    STATE["last_signal"] = {
                        "symbol": sym,
                        "direction": sig["direction"],
                        "score": sig["score"],
                        "setup": sig["setup_name"],
                        "time": database.now_iso()
                    }
                    trade_manager.open_paper_trade(sig, token)

            STATE.update(last_scan_epoch=time.time(), last_error=None)
            time.sleep(config.SCAN_SECONDS)
        except Exception as e:
            STATE.update(status="ERROR", last_error=str(e)[:250])
            time.sleep(max(3, config.SCAN_SECONDS))

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return jsonify(dict(STATE, uptime_seconds=int(time.time() - STARTED))), 200

@app.post("/toggle-bot")
def toggle_bot():
    cur = database.get_setting("auto_trade_enabled", "1")
    new_val = "0" if cur == "1" else "1"
    database.set_setting("auto_trade_enabled", new_val)
    return jsonify({"auto_trade_enabled": new_val == "1"}), 200

@app.get("/vault-data")
def vault_data():
    c = database.db()
    try:
        ts = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()]
        active_rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 10").fetchall()]
        pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()["p"] or 0.0)
        unrealized = float(c.execute("SELECT COALESCE(SUM(unrealized_pnl_usd),0) u FROM trades WHERE stage != 'CLOSED'").fetchone()["u"] or 0.0)

        closed_asc = c.execute("SELECT symbol, realized_pnl_usd, closed_at_utc FROM trades WHERE stage = 'CLOSED' ORDER BY id ASC").fetchall()
        wins = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) > 0]
        losses = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) <= 0]
        total_closed = len(wins) + len(losses)
        win_rate = round((len(wins) / total_closed) * 100.0, 1) if total_closed > 0 else 0.0
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0.0)

        eq = float(config.START_EQUITY)
        peak = eq
        max_dd = 0.0
        equity_curve = [{"trade": 0, "equity": round(eq, 2), "drawdown": 0.0}]
        for idx, r in enumerate(closed_asc, start=1):
            eq += float(r["realized_pnl_usd"] or 0.0)
            if eq > peak:
                peak = eq
            dd = round(((peak - eq) / peak) * 100.0, 2) if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            equity_curve.append({"trade": idx, "equity": round(eq, 2), "drawdown": -dd})

        delta_wallet = trade_manager.fetch_delta_wallet_balance()
        wallet_bal = delta_wallet["balance"] if delta_wallet["balance"] is not None else round(config.START_EQUITY + pnl, 2)
        wallet_avail = delta_wallet["available"] if delta_wallet["available"] is not None else round(wallet_bal, 2)
        auto_enabled = database.get_setting("auto_trade_enabled", "1") == "1"

        markets_info = {}
        for sym in config.SYMBOLS:
            with LOCK:
                m = dict(market_data.get_market(sym))
                st_info = dict(STRUCTURE_CACHE.get(sym, {}))
            params = config.get_asset_params(sym)
            markets_info[sym] = {
                "symbol": sym,
                "display_name": params["display_name"],
                "price": m.get("price"),
                "session": m.get("session", "ACTIVE"),
                "in_killzone": m.get("in_killzone", False),
                "killzone_name": m.get("killzone_name", "OFF_HOURS"),
                "funding_rate": m.get("funding_rate", 0.0001),
                "pdh": m.get("pdh"),
                "pdl": m.get("pdl"),
                "macro_bias": st_info.get("macro_bias", "SCANNING"),
                "adx": st_info.get("adx", 0.0),
                "rsi": st_info.get("rsi", 50.0),
                "cvd_pct": st_info.get("cvd_ratio", 50.0),
                "choch_status": st_info.get("choch_status", "NONE"),
            }

        btc_m = market_data.get_market("BTCUSDT")
        return jsonify({
            "mode": config.MODE,
            "auto_trade_enabled": auto_enabled,
            "uptime_seconds": int(time.time() - STARTED),
            "symbols": config.SYMBOLS,
            "markets": markets_info,
            "wallet": {
                "balance_usd": wallet_bal,
                "available_usd": wallet_avail,
                "is_live_synced": delta_wallet["balance"] is not None
            },
            "price": btc_m.get("price"),
            "session": btc_m.get("session", "ACTIVE"),
            "in_killzone": btc_m.get("in_killzone", False),
            "killzone_name": btc_m.get("killzone_name", "OFF_HOURS"),
            "funding_rate": btc_m.get("funding_rate", 0.0001),
            "pdh": btc_m.get("pdh"),
            "pdl": btc_m.get("pdl"),
            "realized_pnl_usd": round(pnl, 2),
            "unrealized_pnl_usd": round(unrealized, 2),
            "open_trades": len(active_rows),
            "stats": {
                "wins": len(wins),
                "losses": len(losses),
                "total_trades": total_closed,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "max_drawdown_pct": round(max_dd, 2),
                "avg_win_usd": round(gross_win / len(wins), 2) if wins else 0.0,
                "avg_loss_usd": round(-gross_loss / len(losses), 2) if losses else 0.0,
            },
            "equity_curve": equity_curve,
            "vault": ts,
            "active": active_rows
        })
    finally:
        c.close()

if __name__ == "__main__":
    database.init_db()
    threading.Thread(target=notifications.telegram_worker_loop, daemon=True).start()
    threading.Thread(target=engine_supervision_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True)
