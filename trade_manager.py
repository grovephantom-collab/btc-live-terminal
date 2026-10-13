import logging, database, config, notifications

log = logging.getLogger("TradeManager")

def get_equity(conn) -> float:
    # Initial paper balance 500 USD + realized PnL
    r = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as pnl FROM trades").fetchone()
    base = 500.0
    return max(50.0, base + float(r["pnl"] if r else 0.0))

def check_daily_loss(conn) -> bool:
    today = database.now_iso()[:10]
    r = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as pnl FROM trades WHERE opened_at_utc LIKE ?", (today + "%",)).fetchone()
    daily_pnl = float(r["pnl"] if r else 0.0)
    # Safe check: config me chahe variable ho ya na ho, crash nahi hoga
    limit = getattr(config, "DAILY_LOSS_LIMIT_USD", getattr(config, "MAX_DAILY_LOSS", 50.0))
    return daily_pnl <= -abs(float(limit))

def open_paper_trade(signal: dict, fencing_token: int):
    conn = database.db()
    try:
        if check_daily_loss(conn):
            log.warning("Daily risk limit hit. Trade skipped.")
            return

        act = conn.execute("SELECT id FROM trades WHERE stage != 'CLOSED'").fetchone()
        if act:
            return  # Max 1 open position rule intact

        equity = get_equity(conn)
        risk_pct = getattr(config, "RISK_PER_TRADE_PCT", 0.01)
        risk_usd = equity * float(risk_pct)
        entry = float(signal["entry"])
        sl = float(signal["sl"])
        direction = signal["direction"]

        risk_per_coin = abs(entry - sl)
        if risk_per_coin <= 0:
            return

        qty = round(risk_usd / risk_per_coin, 6)
        if qty <= 0:
            return

        tp1 = float(signal["tp1"])
        tp2 = float(signal["tp2"])
        tp3 = float(signal["tp3"])

        sym = getattr(config, "SYMBOL", "BTCUSDT")

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades (
                symbol, direction, entry_price, initial_sl, stop_price,
                tp1_price, tp2_price, tp3_price, qty, original_qty,
                stage, setup_name, opened_at_utc, fencing_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
        """, (
            sym, direction, entry, sl, sl,
            tp1, tp2, tp3, qty, qty,
            signal.get("setup_name", "SMC Setup"), database.now_iso(), fencing_token
        ))
        conn.commit()

        # Instant alert via notifications engine
        notifications.notify_signal_opened({
            "direction": direction,
            "setup_name": signal.get("setup_name", "SMC Setup"),
            "score": signal.get("score", 80),
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "qty": qty
        })
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
        entry = trade["entry_price"]
        sl = trade["stop_price"]
        initial_sl = trade["initial_sl"]
        stage = trade["stage"]
        qty = trade["qty"]
        orig_qty = trade["original_qty"]

        # Safe R-Multiple Calculation (Prevents huge numbers)
        initial_risk_dist = abs(entry - initial_sl)
        def calc_safe_r(exit_px):
            if initial_risk_dist <= 0:
                return 0.0
            raw_r = (exit_px - entry) / initial_risk_dist if direction == "LONG" else (entry - exit_px) / initial_risk_dist
            return max(-5.0, min(round(raw_r, 2), 20.0))

        # 1. Stop Loss Hit
        sl_hit = (current_price <= sl) if direction == "LONG" else (current_price >= sl)
        if sl_hit:
            pnl_usd = (sl - entry) * qty if direction == "LONG" else (entry - sl) * qty
            # Subtract fees & slippage
            pnl_usd -= (sl * qty * 0.001)
            weighted_r = calc_safe_r(sl)

            conn.execute("""
                UPDATE trades SET 
                    stage = 'CLOSED', closed_at_utc = ?, close_reason = 'STOP_LOSS',
                    realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                    weighted_pnl_r = ?, fencing_token = ?
                WHERE id = ?
            """, (database.now_iso(), round(pnl_usd, 2), weighted_r, fencing_token, tid))
            conn.commit()

            notifications.notify_trade_closed({
                "direction": direction,
                "reason": "STOP_LOSS",
                "pnl_usd": round(pnl_usd, 2),
                "r_mult": weighted_r
            })
            return

        # 2. Stage-based TP Management
        tp1 = trade["tp1_price"]
        tp2 = trade["tp2_price"]
        tp3 = trade["tp3_price"]

        # Buffer for zero brokerage on Break-Even
        be_buffer = entry * 0.0008
        be_price = (entry + be_buffer) if direction == "LONG" else (entry - be_buffer)

        # Stage: ACTIVE -> Check TP1 (50% Close + Shift SL to BE)
        if stage == "ACTIVE":
            tp1_hit = (current_price >= tp1) if direction == "LONG" else (current_price <= tp1)
            if tp1_hit:
                part_qty = round(orig_qty * 0.50, 6)
                pnl = (tp1 - entry) * part_qty if direction == "LONG" else (entry - tp1) * part_qty
                pnl -= (tp1 * part_qty * 0.001)
                new_qty = max(0.0, qty - part_qty)

                conn.execute("""
                    UPDATE trades SET 
                        stage = 'TP1_HIT', stop_price = ?, qty = ?,
                        realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                        fencing_token = ?
                    WHERE id = ?
                """, (be_price, new_qty, round(pnl, 2), fencing_token, tid))
                conn.commit()
                return

        # Stage: TP1_HIT -> Check TP2 (30% Close + Lock in TP1 price as SL)
        elif stage == "TP1_HIT":
            tp2_hit = (current_price >= tp2) if direction == "LONG" else (current_price <= tp2)
            if tp2_hit:
                part_qty = round(orig_qty * 0.30, 6)
                pnl = (tp2 - entry) * part_qty if direction == "LONG" else (entry - tp2) * part_qty
                pnl -= (tp2 * part_qty * 0.001)
                new_qty = max(0.0, qty - part_qty)

                conn.execute("""
                    UPDATE trades SET 
                        stage = 'TP2_HIT', stop_price = ?, qty = ?,
                        realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                        fencing_token = ?
                    WHERE id = ?
                """, (tp1, new_qty, round(pnl, 2), fencing_token, tid))
                conn.commit()
                return

        # Stage: TP2_HIT -> Check TP3 / Trailing Runner
        elif stage == "TP2_HIT":
            tp3_hit = (current_price >= tp3) if direction == "LONG" else (current_price <= tp3)
            if tp3_hit:
                trail_sl = tp2
                conn.execute("""
                    UPDATE trades SET 
                        stage = 'RUNNER_TRAILING', stop_price = ?, fencing_token = ?
                    WHERE id = ?
                """, (trail_sl, fencing_token, tid))
                conn.commit()
                return

        # Stage: RUNNER_TRAILING -> Trail behind price dynamically
        elif stage == "RUNNER_TRAILING":
            if direction == "LONG" and current_price > (sl + initial_risk_dist):
                new_sl = current_price - (initial_risk_dist * 0.8)
                if new_sl > sl:
                    conn.execute("UPDATE trades SET stop_price = ?, fencing_token = ? WHERE id = ?", (new_sl, fencing_token, tid))
                    conn.commit()
            elif direction == "SHORT" and current_price < (sl - initial_risk_dist):
                new_sl = current_price + (initial_risk_dist * 0.8)
                if new_sl < sl:
                    conn.execute("UPDATE trades SET stop_price = ?, fencing_token = ? WHERE id = ?", (new_sl, fencing_token, tid))
                    conn.commit()
    finally:
        conn.close()
