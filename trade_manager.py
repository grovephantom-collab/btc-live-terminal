import time, uuid, hmac, hashlib, requests, logging, json
import database, config, notifications
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
    
    # Delta Exchange API v2 requires order_type: "market_order"
    payload = {
        "product_id": config.DELTA_PRODUCT_ID,
        "size": int(max(1, size)),
        "side": side.lower(),
        "order_type": "market_order",
        "bracket_order": {
            "stop_loss_price": str(round(stop_loss, 2)),
            "take_profit_price": str(round(take_profit, 2))
        }
    }
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
            notifications.enqueue_telegram(
                f"🚀 *TRADE OPENED ({signal['direction']})*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Entry: ${entry:,.2f}\n"
                f"• SL: ${sl:,.2f}\n"
                f"• TP1: ${signal['tp1']:,.2f}\n"
                f"• Risk Floor: ${risk_usd:.2f} | Size: {qty} BTC"
            )

        # Execute on Delta Exchange if Mode is TESTNET or LIVE
        if config.MODE in ("TESTNET", "LIVE"):
            delta_side = "buy" if signal["direction"] == "LONG" else "sell"
            place_delta_order(config.SYMBOL, delta_side, int(qty * entry), sl, signal["tp1"])
    finally:
        conn.close()

def manage_positions(current_price: float, fencing_token: int, klines_15m: list = None, current_atr: float = None):
    """
    CRITICAL FIX: Fully manages position lifecycle, Stop Loss hits,
    TP1 / TP2 partial exits, and Break-Even trailing!
    """
    if not current_price or current_price <= 0: return
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token): return
        trade = conn.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 1").fetchone()
        if not trade: return

        trade_dict = dict(trade)
        direction = trade_dict["direction"]
        entry = float(trade_dict["entry_price"])
        sl = float(trade_dict["stop_price"])
        tp1 = float(trade_dict["tp1_price"])
        tp2 = float(trade_dict["tp2_price"])
        rem_qty = float(trade_dict["remaining_qty"])
        init_qty = float(trade_dict["initial_qty"])
        tp1_done = bool(trade_dict["tp1_done"])
        tp2_done = bool(trade_dict["tp2_done"])
        tid = trade_dict["trade_id"]
        now_str = database.now_iso()

        is_long = direction == "LONG"

        # 1. STOP LOSS HIT CHECK
        sl_hit = (is_long and current_price <= sl) or (not is_long and current_price >= sl)
        if sl_hit:
            price_diff = (current_price - entry) if is_long else (entry - current_price)
            loss_usd = round(price_diff * rem_qty, 2)
            total_realized = round(float(trade_dict["realized_pnl_usd"]) + loss_usd, 2)
            close_reason = "BREAK_EVEN_EXIT" if tp1_done else "STOP_LOSS_HIT"

            conn.execute("""
                UPDATE trades
                SET stage = 'CLOSED', remaining_qty = 0, realized_pnl_usd = ?, close_reason = ?, closed_at_utc = ?, updated_at_utc = ?
                WHERE trade_id = ?
            """, (total_realized, close_reason, now_str, now_str, tid))
            conn.commit()

            msg_title = "🛡️ BREAK-EVEN SL HIT" if tp1_done else "🛑 STOP LOSS HIT"
            log.info(f"{msg_title} for {tid} at ${current_price:,.2f} | Realized PnL: ${total_realized:+.2f}")
            if hasattr(notifications, "enqueue_telegram"):
                notifications.enqueue_telegram(
                    f"{msg_title} ({direction})\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• Exit Price: ${current_price:,.2f}\n"
                    f"• Realized PnL: ${total_realized:+.2f} USD\n"
                    f"• Reason: {close_reason}"
                )
            return

        # 2. TAKE PROFIT 1 HIT (Book 50% & Move SL to Break-Even)
        if not tp1_done:
            tp1_hit = (is_long and current_price >= tp1) or (not is_long and current_price <= tp1)
            if tp1_hit:
                booked_qty = round(init_qty * 0.50, 6)
                new_rem_qty = round(rem_qty - booked_qty, 6)
                gain_pts = (tp1 - entry) if is_long else (entry - tp1)
                booked_pnl = round(gain_pts * booked_qty, 2)

                # Move Stop Loss to Break-Even (+/- small buffer)
                be_buffer = getattr(config, "SL_BUFFER", 10.0)
                new_sl = round(entry + be_buffer if is_long else entry - be_buffer, 2)

                conn.execute("""
                    UPDATE trades
                    SET tp1_done = 1, remaining_qty = ?, stop_price = ?, realized_pnl_usd = realized_pnl_usd + ?, updated_at_utc = ?
                    WHERE trade_id = ?
                """, (new_rem_qty, new_sl, booked_pnl, now_str, tid))
                conn.commit()

                log.info(f"TP1 Reached for {tid}! Booked 50% (${booked_pnl:+.2f}), SL moved to Break-Even ${new_sl:,.2f}")
                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(
                        f"🎯 *TARGET 1 REACHED (50% BOOKED)*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• TP1: ${tp1:,.2f}\n"
                        f"• Booked: ${booked_pnl:+.2f} USD\n"
                        f"• SL Trailed to Break-Even: ${new_sl:,.2f}"
                    )
                return

        # 3. TAKE PROFIT 2 HIT (Close Remaining 50%)
        if tp1_done and not tp2_done:
            tp2_hit = (is_long and current_price >= tp2) or (not is_long and current_price <= tp2)
            if tp2_hit:
                gain_pts = (tp2 - entry) if is_long else (entry - tp2)
                final_pnl = round(gain_pts * rem_qty, 2)
                total_realized = round(float(trade_dict["realized_pnl_usd"]) + final_pnl, 2)

                conn.execute("""
                    UPDATE trades
                    SET stage = 'CLOSED', tp2_done = 1, remaining_qty = 0, realized_pnl_usd = ?, close_reason = 'TP2_HIT', closed_at_utc = ?, updated_at_utc = ?
                    WHERE trade_id = ?
                """, (total_realized, now_str, now_str, tid))
                conn.commit()

                log.info(f"TP2 Reached for {tid}! Full position closed with total PnL: ${total_realized:+.2f}")
                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(
                        f"🏁 *TARGET 2 (FULL EXIT)*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• Exit: ${tp2:,.2f}\n"
                        f"• Total Trade PnL: ${total_realized:+.2f} USD"
                    )
                return

        # 4. Update Unrealized PnL
        cur_pnl = round(((current_price - entry) if is_long else (entry - current_price)) * rem_qty, 2)
        conn.execute("UPDATE trades SET unrealized_pnl_usd = ?, updated_at_utc = ? WHERE trade_id = ?", (cur_pnl, now_str, tid))
        conn.commit()

    except Exception as e:
        log.error(f"Error managing position: {e}", exc_info=True)
    finally:
        conn.close()
