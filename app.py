import time, hmac, threading, logging
from flask import Flask, request, jsonify, render_template_string
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
                time.sleep(2); continue

            STATE.update(status="RUNNING_PAPER", fencing_token=token)
            if not market_data.refresh_market_data(LOCK):
                STATE.update(last_error=market_data.MARKET.get("error"))
                time.sleep(config.SCAN_SECONDS); continue

            with LOCK:
                p = market_data.MARKET["price"]
                pts = market_data.MARKET["price_ts"]
                kts = market_data.MARKET["klines_ts"]
                ks = {k: list(v) for k, v in market_data.MARKET["klines"].items()}
                sess = market_data.MARKET["session"]
                poc = market_data.MARKET["poc"]

            if time.time() - pts > config.MAX_DATA_AGE or time.time() - kts > config.MAX_DATA_AGE:
                STATE["status"] = "DATA_STALE"
                time.sleep(config.SCAN_SECONDS); continue

            st = smc_engine.get_structure(ks)
            
            # Wick-Safe & ATR-aware position management call
            trade_manager.manage_positions(p, token, ks.get("15m", []), st.get("atr"))

            sig = smc_engine.evaluate_signal(st, ks["15m"], p, sess, poc)

            if sig:
                STATE["last_signal"] = {"direction": sig["direction"], "score": sig["score"], "setup": sig["setup_name"], "time": database.now_iso()}
                trade_manager.open_paper_trade(sig, token)

            STATE.update(last_scan_epoch=time.time(), last_error=None)
            time.sleep(config.SCAN_SECONDS)
        except Exception as e:
            STATE.update(status="ERROR", last_error=str(e)[:250])
            time.sleep(max(3, config.SCAN_SECONDS))

app = Flask(__name__)

def auth_check() -> bool:
    h = request.headers.get("Authorization", "")
    return h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip().encode(), config.WORKER_AUTH_TOKEN.encode())

@app.before_request
def auth_gate():
    if request.path in ("/healthz", "/", "/dashboard", "/api/public-stats"): 
        return None
    if not auth_check(): 
        return jsonify({"error": "unauthorized"}), 401

@app.get("/healthz")
def healthz():
    with LOCK:
        fresh = bool(market_data.MARKET["price"] and time.time() - market_data.MARKET["price_ts"] <= config.MAX_DATA_AGE)
    d = dict(STATE)
    d.update(mode="PAPER", symbol=config.SYMBOL, market_data_fresh=fresh, uptime_seconds=int(time.time() - STARTED), utc_time=database.now_iso())
    return jsonify(d), (200 if d["status"] in ("RUNNING_PAPER", "STANDBY") else 503)

@app.get("/")
@app.get("/dashboard")
def dashboard_gui():
    c = database.db()
    try:
        ts = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 25").fetchall()]
        act = [dict(r) for r in c.execute("SELECT * FROM trades WHERE stage!='CLOSED' ORDER BY id DESC LIMIT 1").fetchall()]
        pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()["p"] or 0.0)
        
        with LOCK:
            cp = float(market_data.MARKET.get("price") or 0.0)
            cs = str(market_data.MARKET.get("session") or "OFFLINE")
            
        closed = [t for t in ts if t.get("stage") == "CLOSED"]
        wins = sum(1 for t in closed if float(t.get("realized_pnl_usd") or 0) > 0)
        total = len(closed)
        win_rate = round((wins / total * 100), 1) if total > 0 else 0.0

        return jsonify({
            "mode": "PAPER", "price": cp, "session": cs, "realized_pnl_usd": pnl,
            "win_rate": win_rate, "active_trade": act[0] if act else None, "trades": ts
        })
    finally:
        c.close()

if __name__ == "__main__":
    database.init_db()
    threading.Thread(target=notifications.telegram_worker_loop, daemon=True, name="tg_thread").start()
    threading.Thread(target=engine_supervision_loop, daemon=True, name="engine_thread").start()
    log.info(f"Engine cluster online on port {config.PORT}")
    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True, use_reloader=False)
