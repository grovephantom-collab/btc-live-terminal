import time, uuid, logging, database, config, notifications

log = logging.getLogger("TradeManager")

def get_equity(conn) -> float:
    r = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as pnl FROM trades").fetchone()
    base = 500.0
    return max(50.0, base + float(r["pnl"] if r else 0.0))

def check_daily_loss(conn) -> bool:
    today = database.now_iso()[:10]
    r = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as pnl FROM trades WHERE opened_at_utc LIKE ?", (today + "%",)).fetchone()
    daily_pnl = float(r["pnl"] if r else 0.0)
    limit = getattr(config, "DAILY_LOSS_LIMIT_USD", getattr(config, "MAX_DAILY_LOSS", 50.0))
    return daily_pnl <= -abs(float(limit))

def _send_tg(msg: str):
    try:
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(msg)
    except Exception as e:
        log.warning("Telegram alert error: %s", e)

def open_paper_trade(signal: dict, fencing_token: int):
    conn = database.db()
    try:
        if check_daily_loss(conn):
            log.warning("Daily risk limit hit. Trade skipped.")
            return

        act = conn.execute("SELECT id FROM trades WHERE stage != 'CLOSED'").fetchone()
        if act:
            return

        sym = getattr(config, "SYMBOL", "BTCUSDT")
        direction = str(signal["direction"])
        setup_name = str(signal.get("setup_name", "SMC Setup"))
        score = float(signal.get("score", 85.0))

        candle_bucket = int(time.time() // 900)
        sig_key = f"{sym}_{direction}_{candle_bucket}"
        dup = conn.execute("SELECT id FROM trades WHERE signal_key = ?", (sig_key,)).fetchone()
        if dup:
            return

        equity = get_equity(conn)
        risk_pct = getattr(config, "RISK_PER_TRADE_PCT", 0.01)
        risk_usd = equity * float(risk_pct)
        entry = float(signal["entry"])
        sl = float(signal["sl"])

        risk_per_coin = abs(entry - sl)
        if risk_per_coin <= 0:
            return

        qty = round(risk_usd / risk_per_coin, 6)
        if qty <= 0:
            return

        tp1 = float(signal["tp1"])
        tp2 = float(signal["tp2"])
        tp3 = float(signal["tp3"])

        trade_id = f"TRD-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4].upper()}"
        now_str = database.now_iso()

        conn.execute("""
            INSERT INTO trades (
                trade_id, signal_key, symbol, direction, setup_name, score,
                entry_price, stop_price, tp1_price, tp2_price, tp3_price,
                initial_qty, remaining_qty, stage, opened_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
        """, (
            trade_id, sig_key, sym, direction, setup_name, score,
            entry, sl, tp1, tp2, tp3,
            qty, qty, now_str, now_str
        ))
        conn.commit()

        _send_tg(
            f"🚀 NEW {direction} TRADE OPENED ({sym})\n"
            f"Setup: {setup_name} (Score: {score:.0f})\n"
            f"Entry: ${entry:.2f}\n"
            f"Stop Loss: ${sl:.2f}\n"
            f"TP1: ${tp1:.2f} | TP2: ${tp2:.2f} | TP3: ${tp3:.2f}\n"
            f"Size: {qty} BTC"
        )
    finally:
        conn.close()

def manage_positions(current_price: float, fencing_token: int):
    conn = database.db()
    try:
        t = conn.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 1").fetchone()
        if not t:
            return

        trade = dict(t)
        tid = trade["id"]
        direction = trade["direction"]
        entry = float(trade["entry_price"])
        sl = float(trade["stop_price"])
        tp1 = float(trade["tp1_price"])
        tp2 = float(trade["tp2_price"])
        tp3 = float(trade["tp3_price"])
        stage = trade["stage"]
        qty = float(trade["remaining_qty"])
        orig_qty = float(trade["initial_qty"])
        realized_so_far = float(trade.get("realized_pnl_usd") or 0.0)
        now_str = database.now_iso()

        initial_risk_dist = abs(tp1 - entry) / 1.5
        if initial_risk_dist <= 0:
            initial_risk_dist = max(abs(entry - sl), 1.0)
        initial_risk_usd = initial_risk_dist * orig_qty

        def calc_safe_r(exit_px):
            if initial_risk_dist <= 0:
                return 0.0
            raw_r = (exit_px - entry) / initial_risk_dist if direction == "LONG" else (entry - exit_px) / initial_risk_dist
            return max(-5.0, min(round(raw_r, 2), 20.0))

        # 1. Stop Loss Hit
        sl_hit = (current_price <= sl) if direction == "LONG" else (current_price >= sl)
        if sl_hit:
            pnl_usd = (sl - entry) * qty if direction == "LONG" else (entry - sl) * qty
            fee = sl * qty * 0.001
            pnl_usd -= fee
            total_pnl = realized_so_far + pnl_usd

            weighted_r = max(-5.0, min(round(total_pnl / initial_risk_usd, 2), 20.0)) if initial_risk_usd > 0 else 0.0

            if stage == "RUNNER_TRAILING":
                reason = "TRAIL_STOP_WIN"
            elif stage == "TP2_HIT":
                reason = "TP1_LOCK_WIN"
            elif stage == "TP1_HIT":
                reason = "BREAKEVEN_EXIT"
            else:
                reason = "STOP_LOSS"

            conn.execute("""
                UPDATE trades SET 
                    stage = 'CLOSED', remaining_qty = 0, closed_at_utc = ?, updated_at_utc = ?,
                    close_reason = ?,
                    realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                    total_fee_usd = COALESCE(total_fee_usd, 0) + ?,
                    weighted_pnl_r = ?
                WHERE id = ?
            """, (now_str, now_str, reason, round(pnl_usd, 2), round(fee, 2), weighted_r, tid))
            conn.commit()

            _send_tg(
                f"🏁 TRADE CLOSED ({direction})\n"
                f"Reason: {reason}\n"
                f"Exit Price: ${sl:.2f}\n"
                f"Total Net PnL: ${total_pnl:+.2f} ({weighted_r:+.2f}R)"
            )
            return

        # True Brokerage-Buffer Break-Even Price (Covers 0.1% exchange entry/exit fees)
        be_buffer = entry * 0.0012
        be_price = (entry + be_buffer) if direction == "LONG" else (entry - be_buffer)

        # Stage: ACTIVE / OPEN -> Check TP1 (50% Close + Shift SL to Brokerage-Safe BE)
        if stage in ("ACTIVE", "OPEN"):
            tp1_hit = (current_price >= tp1) if direction == "LONG" else (current_price <= tp1)
            if tp1_hit:
                part_qty = round(orig_qty * 0.50, 6)
                pnl = (tp1 - entry) * part_qty if direction == "LONG" else (entry - tp1) * part_qty
                fee = tp1 * part_qty * 0.001
                pnl -= fee
                new_qty = max(0.0, round(qty - part_qty, 6))

                conn.execute("""
                    UPDATE trades SET 
                        stage = 'TP1_HIT', tp1_done = 1, stop_price = ?, remaining_qty = ?,
                        realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                        total_fee_usd = COALESCE(total_fee_usd, 0) + ?,
                        updated_at_utc = ?
                    WHERE id = ?
                """, (round(be_price, 2), new_qty, round(pnl, 2), round(fee, 2), now_str, tid))
                conn.commit()

                _send_tg(
                    f"✅ TP1 HIT ({direction})\n"
                    f"50% Position Booked: ${pnl:+.2f}\n"
                    f"SL Shifted to Fee-Safe BE: ${be_price:.2f}"
                )
                return

        # Stage: TP1_HIT -> Check TP2 (30% Close + Lock in TP1 price as SL)
        elif stage == "TP1_HIT":
            tp2_hit = (current_price >= tp2) if direction == "LONG" else (current_price <= tp2)
            if tp2_hit:
                part_qty = round(orig_qty * 0.30, 6)
                pnl = (tp2 - entry) * part_qty if direction == "LONG" else (entry - tp2) * part_qty
                fee = tp2 * part_qty * 0.001
                pnl -= fee
                new_qty = max(0.0, round(qty - part_qty, 6))

                conn.execute("""
                    UPDATE trades SET 
                        stage = 'TP2_HIT', tp2_done = 1, stop_price = ?, remaining_qty = ?,
                        realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                        total_fee_usd = COALESCE(total_fee_usd, 0) + ?,
                        updated_at_utc = ?
                    WHERE id = ?
                """, (tp1, new_qty, round(pnl, 2), round(fee, 2), now_str, tid))
                conn.commit()

                _send_tg(
                    f"🔥 TP2 HIT ({direction})\n"
                    f"30% Position Booked: ${pnl:+.2f}\n"
                    f"SL Locked at TP1: ${tp1:.2f}"
                )
                return

        # Stage: TP2_HIT -> Check TP3 / Trailing Runner
        elif stage == "TP2_HIT":
            tp3_hit = (current_price >= tp3) if direction == "LONG" else (current_price <= tp3)
            if tp3_hit:
                conn.execute("""
                    UPDATE trades SET 
                        stage = 'RUNNER_TRAILING', tp3_done = 1, stop_price = ?, updated_at_utc = ?
                    WHERE id = ?
                """, (tp2, now_str, tid))
                conn.commit()

                _send_tg(
                    f"🏆 TP3 HIT ({direction})\n"
                    f"Runner (20%) Now Trailing! SL Locked at TP2: ${tp2:.2f}"
                )
                return

        # Stage: RUNNER_TRAILING -> Trail behind price dynamically
        elif stage == "RUNNER_TRAILING":
            if direction == "LONG" and current_price > (sl + initial_risk_dist):
                new_sl = round(current_price - (initial_risk_dist * 0.8), 2)
                if new_sl > sl:
                    conn.execute("UPDATE trades SET stop_price = ?, updated_at_utc = ? WHERE id = ?", (new_sl, now_str, tid))
                    conn.commit()
            elif direction == "SHORT" and current_price < (sl - initial_risk_dist):
                new_sl = round(current_price + (initial_risk_dist * 0.8), 2)
                if new_sl < sl:
                    conn.execute("UPDATE trades SET stop_price = ?, updated_at_utc = ? WHERE id = ?", (new_sl, now_str, tid))
                    conn.commit()
    finally:
        conn.close()
