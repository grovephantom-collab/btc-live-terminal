import time, uuid, hmac, hashlib, requests, logging, json
import database, config, notifications
from datetime import datetime, timezone

log = logging.getLogger("TradeManager")
HTTP = requests.Session()

DELTA_BASE_URL = "https://testnet-api.delta.exchange" if config.DELTA_TESTNET else "https://api.delta.exchange"

def _get_delta_signature(secret: str, method: str, path: str, payload_str: str, timestamp: str) -> str:
    message = timestamp + method + path + payload_str
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

def place_delta_order(symbol: str, side: str, lots: int, stop_loss: float, take_profit: float):
    if config.MODE == "PAPER":
        return None
    if not config.DELTA_API_KEY or not config.DELTA_API_SECRET:
        log.warning("Delta API credentials missing. Skipping exchange execution.")
        return None

    path = "/v2/orders"
    url = f"{DELTA_BASE_URL}{path}"
    timestamp = str(int(time.time()))
    
    payload = {
        "product_id": config.DELTA_PRODUCT_ID,
        "size": int(max(1, lots)),
        "side": side.lower(),
        "order_type": "market_order",
        "bracket_order": {
            "stop_loss_price": str(round(stop_loss, 2)),
            "take_profit_price": str(round(take_profit, 2))
        }
    }
    payload_str = json.dumps(payload, separators=(",", ":"))
    signature = _get_delta_signature(config.DELTA_API_SECRET, "POST", path, payload_str, timestamp)
    headers = {
        "api-key": config.DELTA_API_KEY,
        "signature": signature,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }
    
    try:
        r = HTTP.post(url, headers=headers, data=payload_str, timeout=10)
        if r.status_code in (200, 201):
            log.info("Delta Order Placed (%d lots): %s", lots, r.json())
            return r.json()
        else:
            log.error("Delta Order Failed [%s]: %s", r.status_code, r.text)
    except Exception as e:
        log.error("Delta API Exception: %s", e)
    return None

def get_equity(conn) -> float:
    r = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as pnl FROM trades").fetchone()
    return max(4.0, config.START_EQUITY + float(r["pnl"] if r else 0.0))

def _check_cooldown_and_circuit_breakers(conn) -> tuple:
    now_dt = datetime.now(timezone.utc)
    today_prefix = now_dt.date().isoformat()

    daily_rows = conn.execute(
        "SELECT realized_pnl_usd, stage, close_reason FROM trades WHERE opened_at_utc LIKE ?",
        (f"{today_prefix}%",)
    ).fetchall()
    
    if len(daily_rows) >= getattr(config, "MAX_DAILY_TRADES", 3):
        return False, "Max daily trades (3) reached. Capital preserved."

    daily_pnl = sum(float(r["realized_pnl_usd"] or 0.0) for r in daily_rows)
    max_daily_loss = getattr(config, "MAX_DAILY_LOSS", 2.50)
    if daily_pnl <= -abs(max_daily_loss):
        return False, f"Daily Loss Limit hit (${daily_pnl:.2f}). Trading paused for today."

    recent_closed = conn.execute(
        "SELECT close_reason, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND closed_at_utc IS NOT NULL ORDER BY id DESC LIMIT 2"
    ).fetchall()

    # 2 Consecutive SL Hits -> 2 Hours Mandatory Trading Pause
    if len(recent_closed) >= 2:
        reasons = [r["close_reason"] or "" for r in recent_closed]
        if all("STOP_LOSS" in r for r in reasons):
            try:
                last_dt = datetime.fromisoformat(recent_closed[0]["closed_at_utc"])
                elapsed_pause = (now_dt - last_dt).total_seconds()
                pause_limit = getattr(config, "CONSECUTIVE_SL_PAUSE_SEC", 7200)
                if elapsed_pause < pause_limit:
                    rem_m = int((pause_limit - elapsed_pause) // 60)
                    return False, f"Circuit Breaker: 2 Consecutive SLs hit -> Paused for {rem_m}m to avoid chop."
            except Exception:
                pass

    if recent_closed and recent_closed[0]["closed_at_utc"]:
        try:
            closed_dt = datetime.fromisoformat(recent_closed[0]["closed_at_utc"])
            elapsed = (now_dt - closed_dt).total_seconds()
            reason = recent_closed[0]["close_reason"] or ""
            sl_cooldown = getattr(config, "SL_COOLDOWN_SECONDS", 3600)
            min_cooldown = getattr(config, "TRADE_COOLDOWN_SECONDS", 1200)

            if "STOP_LOSS" in reason and elapsed < sl_cooldown:
                rem_m = int((sl_cooldown - elapsed) // 60)
                return False, f"SL Cooldown active ({rem_m}m remaining). Waiting for market stabilization."
            if elapsed < min_cooldown:
                rem_m = int((min_cooldown - elapsed) // 60)
                return False, f"Trade Cooldown active ({rem_m}m remaining)."
        except Exception:
            pass

    return True, "OK"

def calculate_auto_compounding_size(equity_usd: float, entry: float, sl: float) -> tuple:
    risk_per_btc = abs(entry - sl)
    if risk_per_btc <= 0:
        return 0.0, 0, 0.0

    lot_size_btc = getattr(config, "DELTA_LOT_SIZE_BTC", 0.001)
    max_lots = getattr(config, "MAX_LOTS_CAP", 5)
    min_risk = getattr(config, "MIN_RISK_USD", 0.35)
    max_risk = getattr(config, "MAX_RISK_USD", 1.20)

    target_risk = max(min_risk, min(max_risk * (equity_usd / 12.0), equity_usd * float(config.RISK_PCT)))
    raw_qty_btc = target_risk / risk_per_btc

    lots = max(1, min(max_lots, int(round(raw_qty_btc / lot_size_btc))))
    qty_btc = round(lots * lot_size_btc, 4)
    actual_risk_usd = round(qty_btc * risk_per_btc, 2)

    return qty_btc, lots, actual_risk_usd

def open_paper_trade(signal: dict, fencing_token: int):
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token):
            return
        act = conn.execute("SELECT id FROM trades WHERE stage != 'CLOSED'").fetchone()
        if act:
            return

        allowed, reason = _check_cooldown_and_circuit_breakers(conn)
        if not allowed:
            log.info("Trade skipped by Circuit Breaker: %s", reason)
            return

        entry, sl = float(signal["entry"]), float(signal["sl"])
        if abs(entry - sl) <= 0:
            return

        candle_ts = int(signal.get("candle_time") or (int(time.time()) // 900) * 900)
        sig_key = f"{config.SYMBOL}_{signal['direction']}_{candle_ts}"
        existing_sig = conn.execute("SELECT id FROM trades WHERE signal_key = ?", (sig_key,)).fetchone()
        if existing_sig:
            return

        equity = get_equity(conn)
        qty, lots, risk_usd = calculate_auto_compounding_size(equity, entry, sl)
        if qty <= 0 or lots <= 0:
            return

        trade_id = f"TRD-{int(time.time() * 1000)}"
        now_str = database.now_iso()
        inr_rate = getattr(config, "USD_INR_RATE", 86.0)
        risk_inr = round(risk_usd * inr_rate, 0)
        sl_points = round(abs(entry - sl), 1)

        conn.execute("""
            INSERT INTO trades (trade_id, signal_key, symbol, direction, setup_name, score, entry_price, stop_price, tp1_price, tp2_price, tp3_price, initial_qty, remaining_qty, stage, opened_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
        """, (
            trade_id, sig_key, config.SYMBOL, signal["direction"], signal["setup_name"],
            signal["score"], entry, sl, signal["tp1"], signal["tp2"], signal["tp3"],
            qty, qty, now_str, now_str
        ))
        conn.commit()

        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(
                f"🚀 *INSTITUTIONAL TRADE OPENED ({signal['direction']})*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Setup: {signal['setup_name']}\n"
                f"• Entry: ${entry:,.2f} | SL: ${sl:,.2f} ({sl_points} pts)\n"
                f"• TP1: ${signal['tp1']:,.2f} | TP2: ${signal['tp2']:,.2f} | Runner: ${signal['tp3']:,.2f}\n"
                f"• Delta Size: {lots} Lot ({qty} BTC)\n"
                f"• Max Risk: ${risk_usd:.2f} (~₹{risk_inr:.0f})\n"
                f"• Account Equity: ${equity:.2f} USD"
            )

        if config.MODE in ("TESTNET", "LIVE"):
            delta_side = "buy" if signal["direction"] == "LONG" else "sell"
            place_delta_order(config.SYMBOL, delta_side, lots, sl, signal["tp2"])
    finally:
        conn.close()

def manage_positions(current_price: float, fencing_token: int, klines_15m: list = None, current_atr: float = None):
    if not current_price or current_price <= 0:
        return
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token):
            return
        trade = conn.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 1").fetchone()
        if not trade:
            return

        trade_dict = dict(trade)
        direction = trade_dict["direction"]
        entry = float(trade_dict["entry_price"])
        sl = float(trade_dict["stop_price"])
        tp1 = float(trade_dict["tp1_price"])
        tp2 = float(trade_dict["tp2_price"])
        tp3 = float(trade_dict["tp3_price"])
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
            exec_exit = sl if config.MODE == "PAPER" else current_price
            price_diff = (exec_exit - entry) if is_long else (entry - exec_exit)
            fee_usd = round((entry + exec_exit) * rem_qty * float(config.FEE_RATE), 3)
            leg_pnl = round((price_diff * rem_qty) - fee_usd, 2)
            total_realized = round(float(trade_dict["realized_pnl_usd"]) + leg_pnl, 2)
            close_reason = "BREAK_EVEN_EXIT" if tp1_done else "STOP_LOSS_HIT"

            conn.execute("""
                UPDATE trades
                SET stage = 'CLOSED', remaining_qty = 0, realized_pnl_usd = ?, unrealized_pnl_usd = 0,
                    total_fee_usd = total_fee_usd + ?, close_reason = ?, closed_at_utc = ?, updated_at_utc = ?
                WHERE trade_id = ?
            """, (total_realized, fee_usd, close_reason, now_str, now_str, tid))
            conn.commit()

            msg_title = "🛡️ BREAK-EVEN SL HIT (PROFIT SECURED)" if tp1_done else "🛑 STOP LOSS HIT"
            log.info("%s for %s at $%.2f | Realized PnL: $%+.2f", msg_title, tid, exec_exit, total_realized)
            if hasattr(notifications, "enqueue_telegram"):
                notifications.enqueue_telegram(
                    f"{msg_title} ({direction})\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• Exit Price: ${exec_exit:,.2f}\n"
                    f"• Net PnL: ${total_realized:+.2f} USD\n"
                    f"• Reason: {close_reason}"
                )
            return

        # 2. TAKE PROFIT 1 HIT (1.4R -> Book 50% & Move SL to Entry + 25 pts)
        if not tp1_done:
            tp1_hit = (is_long and current_price >= tp1) or (not is_long and current_price <= tp1)
            if tp1_hit:
                booked_qty = round(init_qty * 0.50, 6)
                new_rem_qty = max(0.0, round(rem_qty - booked_qty, 6))
                gain_pts = (tp1 - entry) if is_long else (entry - tp1)
                fee_usd = round((entry + tp1) * booked_qty * float(config.FEE_RATE), 3)
                booked_pnl = round((gain_pts * booked_qty) - fee_usd, 2)

                be_offset = 25.0
                new_sl = round(entry + be_offset if is_long else entry - be_offset, 2)

                conn.execute("""
                    UPDATE trades
                    SET tp1_done = 1, remaining_qty = ?, stop_price = ?,
                        realized_pnl_usd = realized_pnl_usd + ?, total_fee_usd = total_fee_usd + ?, updated_at_utc = ?
                    WHERE trade_id = ?
                """, (new_rem_qty, new_sl, booked_pnl, fee_usd, now_str, tid))
                conn.commit()

                log.info("TP1 Reached for %s! Booked 50%% ($%+.2f), SL moved to BE $%.2f", tid, booked_pnl, new_sl)
                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(
                        f"🎯 *TARGET 1 HIT (50% BOOKED)*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• TP1: ${tp1:,.2f}\n"
                        f"• Locked Profit: ${booked_pnl:+.2f} USD\n"
                        f"• SL Trailed to Zero-Risk BE: ${new_sl:,.2f}"
                    )
                return

        # 3. TAKE PROFIT 2 HIT (2.4R -> Book 30% & Trail SL to TP1 level)
        if tp1_done and not tp2_done:
            tp2_hit = (is_long and current_price >= tp2) or (not is_long and current_price <= tp2)
            if tp2_hit:
                booked_qty = round(init_qty * 0.30, 6)
                new_rem_qty = max(0.0, round(rem_qty - booked_qty, 6))
                gain_pts = (tp2 - entry) if is_long else (entry - tp2)
                fee_usd = round((entry + tp2) * booked_qty * float(config.FEE_RATE), 3)
                booked_pnl = round((gain_pts * booked_qty) - fee_usd, 2)

                new_sl = round(tp1, 2)

                conn.execute("""
                    UPDATE trades
                    SET tp2_done = 1, remaining_qty = ?, stop_price = ?,
                        realized_pnl_usd = realized_pnl_usd + ?, total_fee_usd = total_fee_usd + ?, updated_at_utc = ?
                    WHERE trade_id = ?
                """, (new_rem_qty, new_sl, booked_pnl, fee_usd, now_str, tid))
                conn.commit()

                log.info("TP2 Reached for %s! Booked 30%% ($%+.2f), SL trailed to TP1 $%.2f for runner", tid, booked_pnl, new_sl)
                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(
                        f"🎯 *TARGET 2 HIT (30% BOOKED)*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• TP2: ${tp2:,.2f}\n"
                        f"• Locked Profit: ${booked_pnl:+.2f} USD\n"
                        f"• SL Trailed to TP1: ${new_sl:,.2f}\n"
                        f"• 20% Runner Trailing for Target 3 (${tp3:,.2f})!"
                    )
                return

        # 4. RUNNER FULL TARGET (TP3 3.5R -> 100% Exit)
        if tp1_done and tp2_done:
            tp3_hit = (is_long and current_price >= tp3) or (not is_long and current_price <= tp3)
            if tp3_hit:
                gain_pts = (tp3 - entry) if is_long else (entry - tp3)
                fee_usd = round((entry + tp3) * rem_qty * float(config.FEE_RATE), 3)
                final_pnl = round((gain_pts * rem_qty) - fee_usd, 2)
                total_realized = round(float(trade_dict["realized_pnl_usd"]) + final_pnl, 2)

                conn.execute("""
                    UPDATE trades
                    SET stage = 'CLOSED', remaining_qty = 0, realized_pnl_usd = ?,
                        unrealized_pnl_usd = 0, total_fee_usd = total_fee_usd + ?, close_reason = 'TP3_RUNNER_HIT',
                        closed_at_utc = ?, updated_at_utc = ?
                    WHERE trade_id = ?
                """, (total_realized, fee_usd, now_str, now_str, tid))
                conn.commit()

                log.info("TP3 Runner Reached for %s! Total Trade PnL: $%+.2f", tid, total_realized)
                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(
                        f"🏆 *TARGET 3 (RUNNER FULL EXIT)*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• Final Target: ${tp3:,.2f}\n"
                        f"• Total Blockbuster PnL: ${total_realized:+.2f} USD"
                    )
                return

        cur_pnl = round(((current_price - entry) if is_long else (entry - current_price)) * rem_qty, 2)
        conn.execute("UPDATE trades SET unrealized_pnl_usd = ?, updated_at_utc = ? WHERE trade_id = ?", (cur_pnl, now_str, tid))
        conn.commit()

    except Exception as e:
        log.error("Error managing position: %s", e, exc_info=True)
    finally:
        conn.close()
