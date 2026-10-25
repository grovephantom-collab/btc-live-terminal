import time, threading, logging
from flask import Flask, request, jsonify
import config, database, market_data, smc_engine, trade_manager, notifications

log = logging.getLogger("Supervisor")
LOCK = threading.Lock()
STARTED = time.time()
STATE = {"status": "STARTING", "last_scan_epoch": 0, "last_signal": None, "last_error": None, "fencing_token": None}

def engine_supervision_loop():
    while True:
        try:
            is_leader, token = database.lease_tick()
            if not is_leader or token is None:
                STATE.update(status="STANDBY", fencing_token=None)
                time.sleep(2)
                continue

            STATE.update(status="RUNNING", fencing_token=token)
            if not market_data.refresh_market_data(LOCK):
                STATE.update(last_error=market_data.MARKET.get("error"))
                time.sleep(config.SCAN_SECONDS)
                continue

            with LOCK:
                p = market_data.MARKET["price"]
                ks = {k: list(v) for k, v in market_data.MARKET["klines"].items()}
                sess = market_data.MARKET["session"]
                poc = market_data.MARKET["poc"]

            st = smc_engine.get_structure(ks)
            trade_manager.manage_positions(p, token, ks.get("15m", []), st.get("atr"))
            sig = smc_engine.evaluate_signal(st, ks.get("15m", []), p, sess, poc)

            if sig:
                STATE["last_signal"] = {
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

@app.get("/vault-data")
def vault_data():
    c = database.db()
    try:
        ts = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 25").fetchall()]
        active_rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 1").fetchall()]
        pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()["p"] or 0.0)

        closed_rows = c.execute("SELECT realized_pnl_usd FROM trades WHERE stage = 'CLOSED'").fetchall()
        wins = sum(1 for r in closed_rows if float(r["realized_pnl_usd"] or 0.0) > 0)
        losses = sum(1 for r in closed_rows if float(r["realized_pnl_usd"] or 0.0) <= 0)
        total_closed = wins + losses
        win_rate = round((wins / total_closed) * 100.0, 1) if total_closed > 0 else 0.0

        return jsonify({
            "mode": config.MODE,
            "price": market_data.MARKET.get("price"),
            "session": market_data.MARKET.get("session", "ACTIVE"),
            "realized_pnl_usd": round(pnl, 2),
            "open_trades": len(active_rows),
            "stats": {
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "total_r": 0.0
            },
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
