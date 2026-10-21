import time, uuid, hmac, hashlib, requests, logging, database, config, notifications
from datetime import datetime, timezone

log = logging.getLogger("TradeManager")
HTTP = requests.Session()

DELTA_BASE_URL = "https://testnet-api.delta.exchange" if config.DELTA_TESTNET else "https://api.delta.exchange"

def _get_delta_signature(secret: str, method: str, path: str, payload_str: str, timestamp: str) -> str:
    message = timestamp + method + path + payload_str
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

def place_delta_order(symbol: str, side: str, size: int, stop_loss: float, take_profit: float):
    if config.MODE == "PAPER": return None
    if not config.DELTA_API_KEY or not config.DELTA_API_SECRET:
        log.warning("Delta API credentials missing. Skipping exchange execution.")
        return None

    path = "/v2/orders"
    url = f"{DELTA_BASE_URL}{path}"
    timestamp = str(int(time.time()))
    payload = {
        "product_id": config.DELTA_PRODUCT_ID,
        "size": int(size),
        "side": side.lower(),
        "order_type": "market",
        "stop_loss_price": str(stop_loss),
        "take_profit_price": str(take_profit)
    }
    import json
    payload_str = json.dumps(payload, separators=(",", ":"))
    signature = _get_delta_signature(config.DELTA_API_SECRET, "POST", path, payload_str, timestamp)
    headers = {"api-key": config.DELTA_API_KEY, "signature": signature, "timestamp": timestamp, "Content-Type": "application/json"}
    
    try:
        r = HTTP.post(url, headers=headers, data=payload_str, timeout=10)
        if r.status_code in (200, 201):
            log.info("Delta Order Placed: %s", r.json())
            return r.json()
        else:
            log.error("Delta Order Failed [%s]: %s", r.status_code, r.text)
    except Exception as e:
        log.error("Delta API Exception: %s", e)
    return None

def get_equity(conn) -> float:
    r = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as pnl FROM trades").fetchone()
    return max(50.0, config.START_EQUITY + float(r["pnl"] if r else 0.0))

def open_paper_trade(signal: dict, fencing_token: int):
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token): return
        act = conn.execute("SELECT id FROM trades WHERE stage != 'CLOSED'").fetchone()
        if act: return

        equity = get_equity(conn)
        calculated_risk = equity * float(config.RISK_PCT)
        
        # --- $5 MINIMUM RISK FLOOR ---
        risk_usd = max(5.0, calculated_risk)

        entry, sl = float(signal["entry"]), float(signal["sl"])
        risk_per_coin = abs(entry - sl)
        if risk_per_coin <= 0: return

        qty = round(risk_usd / risk_per_coin, 6)
        if qty <= 0: return

        trade_id = f"TRD-{int(time.time() * 1000)}"
        now_str = database.now_iso()

        conn.execute("""
            INSERT INTO trades (trade_id, signal_key, symbol, direction, setup_name, score, entry_price, stop_price, tp1_price, tp2_price, tp3_price, initial_qty, remaining_qty, stage, opened_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
        """, (trade_id, f"{config.SYMBOL}_{int(time.time())}", config.SYMBOL, signal["direction"], signal["setup_name"], signal["score"], entry, sl, signal["tp1"], signal["tp2"], signal["tp3"], qty, qty, now_str, now_str))
        conn.commit()

        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"🚀 TRADE OPENED ({signal['direction']})\nRisk Floor: ${risk_usd:.2f} | Size: {qty} BTC")

        # Execute on Delta Exchange if Mode is TESTNET or LIVE
        if config.MODE in ("TESTNET", "LIVE"):
            delta_side = "buy" if signal["direction"] == "LONG" else "sell"
            place_delta_order(config.SYMBOL, delta_side, int(qty * entry), sl, signal["tp1"])
    finally:
        conn.close()

def manage_positions(current_price: float, fencing_token: int, klines_15m: list = None, current_atr: float = None):
    # Basic position lifecycle management placeholder
    pass
