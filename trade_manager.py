import math, uuid, sqlite3
import config, database
from notifications import enqueue_telegram

def get_equity(c: sqlite3.Connection) -> float:
    r = c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()
    return max(0, config.START_EQUITY + float(r["p"] or 0.0))

def get_daily_stats(c: sqlite3.Connection):
    day = database.today()
    rows = c.execute("SELECT realized_pnl_usd FROM trades WHERE substr(opened_at_utc,1,10)=?", (day,)).fetchall()
    cnt = c.execute("SELECT COUNT(*) n FROM trades WHERE substr(opened_at_utc,1,10)=?", (day,)).fetchone()["n"]
    return sum(float(r["realized_pnl_usd"] or 0.0) for r in rows), int(cnt or 0)

# 19, 20, 21, 22, 23 & 28. Risk Calculations & Idempotent Keys
def open_paper_trade(s: dict, token: int) -> bool:
    if not database.leader_ok(token): return False
    key = f"{config.SYMBOL}:{s['direction']}:{s['trigger_time']}:{s['swept_level']}"
    c = database.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM signals WHERE signal_key=?", (key,)).fetchone():
            c.rollback(); return False
        
        # 21 & 23. Daily Drawdown Limit & Max Trades
        pnl, n = get_daily_stats(c)
        op = c.execute("SELECT COUNT(*) n FROM trades WHERE stage!='CLOSED'").fetchone()["n"]
        if pnl <= -config.MAX_DAILY_LOSS or n >= config.MAX_DAILY_TRADES or op >= config.MAX_OPEN_TRADES:
            c.rollback(); return False

        # 22. 2 Consecutive SL -> 24h Lockout
        recent = c.execute("SELECT close_reason, closed_at_utc FROM trades WHERE stage='CLOSED' ORDER BY id DESC LIMIT 2").fetchall()
        if len(recent) >= 2 and all(r["close_reason"] == "STOP_LOSS" for r in recent):
            from datetime import datetime, timezone
            last_close = recent[0]["closed_at_utc"]
            if last_close and (datetime.now(timezone.utc) - datetime.fromisoformat(last_close)).total_seconds() < 86400:
                c.rollback(); return False

        # 20. 1% Account Equity Dynamic Sizing
        qty = min((get_equity(c) * config.RISK_PCT) / s["risk"], config.MAX_NOTIONAL / s["entry"])
        if not math.isfinite(qty) or qty <= 0: c.rollback(); return False

        c.execute("""INSERT INTO signals(signal_key,symbol,direction,setup_name,score,entry_price,stop_price,tp1_price,tp2_price,tp3_price,timeframe,candle_time,details_json,created_at_utc)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (key, config.SYMBOL, s["direction"], s["setup_name"], s["score"], s["entry"], s["sl"], s["tp1"], s["tp2"], s["tp3"], s["timeframe"], s["trigger_time"], database.jd(s), database.now_iso()))

        tid, now = str(uuid.uuid4()), database.now_iso()
        c.execute("""INSERT INTO trades(trade_id,signal_key,symbol,direction,setup_name,score,entry_price,stop_price,tp1_price,tp2_price,tp3_price,initial_qty,remaining_qty,stage,opened_at_utc,updated_at_utc)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)""",
                  (tid, key, config.SYMBOL, s["direction"], s["setup_name"], s["score"], s["entry"], s["sl"], s["tp1"], s["tp2"], s["tp3"], qty, qty, now, now))
        database.audit(c, tid, "PAPER_OPEN", {"qty": qty, "fencing_token": token, "signal_key": key})
        c.commit()

        enqueue_telegram(f"PAPER {config.SYMBOL} {s['direction']}\n{s['setup_name']}\nScore: {s['score']:.1f}\nEntry: {s['entry']:.2f} | SL: {s['sl']:.2f}\nTPs: {s['tp1']:.2f} / {s['tp2']:.2f} / {s['tp3']:.2f}\nQty: {qty:.6f}")
        return True
    except Exception:
        c.rollback(); return False
    finally: c.close()

# 24, 25, 26 & 27. Partials, Breakeven & Fee Accounting
def manage_positions(price: float, token: int):
    if not database.leader_ok(token) or price <= 0: return
    c = database.db()
    try:
        c.execute("BEGIN IMMEDIATE")
        for r in c.execute("SELECT * FROM trades WHERE stage!='CLOSED'").fetchall():
            d = r["direction"]
            sign = 1 if d == "LONG" else -1
            entry, stop, qty = float(r["entry_price"]), float(r["stop_price"]), float(r["remaining_qty"])
            realized, fees, stage = float(r["realized_pnl_usd"] or 0.0), float(r["total_fee_usd"] or 0.0), r["stage"]
            flags = {"tp1": r["tp1_done"], "tp2": r["tp2_done"], "tp3": r["tp3_done"]}
            reason = None

            stop_hit = price <= stop if d == "LONG" else price >= stop
            if stop_hit:
                # 27. Fee & Slippage Deduction
                exitp = stop * (1 - config.SLIPPAGE if sign == 1 else 1 + config.SLIPPAGE)
                gross = (exitp - entry) * qty * sign
                fee = abs(exitp * qty) * config.FEE_RATE
                realized += gross - fee; fees += fee; qty = 0; reason = "STOP_LOSS"
            else:
                # 24. Multi-Stage Partials (50% / 30% / 20%)
                for idx, field, frac in [(1, "tp1_price", 0.50), (2, "tp2_price", 0.30), (3, "tp3_price", 1.00)]:
                    fl = f"tp{idx}"
                    hit = price >= float(r[field]) if d == "LONG" else price <= float(r[field])
                    if flags[fl] or not hit: continue
                    close_qty = qty if idx == 3 else min(qty, float(r["initial_qty"]) * frac)
                    if close_qty <= 0: continue
                    exitp = float(r[field]) * (1 - config.SLIPPAGE if sign == 1 else 1 + config.SLIPPAGE)
                    gross = (exitp - entry) * close_qty * sign
                    fee = abs(exitp * close_qty) * config.FEE_RATE
                    realized += gross - fee; fees += fee; qty = max(0, qty - close_qty); flags[fl] = 1
                    
                    # 25. Auto-Breakeven Stop Shift at TP1
                    if idx == 1: stop = entry; stage = "TP1_HIT"
                    elif idx == 2: stage = "TP2_HIT"
                    else: reason = "TP3"; qty = 0
                    break

            if qty <= 1e-10: qty = 0; stage = "CLOSED"; reason = reason or "TARGETS_COMPLETE"
            unreal = (price - entry) * qty * sign
            risk = max(float(r["initial_qty"]) * abs(entry - float(r["stop_price"])), 1e-9)
            # 26. Weighted Realized R
            weighted_r = realized / risk

            c.execute("""UPDATE trades SET stop_price=?,remaining_qty=?,tp1_done=?,tp2_done=?,tp3_done=?,stage=?,realized_pnl_usd=?,unrealized_pnl_usd=?,total_fee_usd=?,weighted_pnl_r=?,close_reason=?,closed_at_utc=?,updated_at_utc=? WHERE trade_id=?""",
                      (stop, qty, flags["tp1"], flags["tp2"], flags["tp3"], stage, realized, unreal, fees, weighted_r, reason, database.now_iso() if stage == "CLOSED" else None, database.now_iso(), r["trade_id"]))
            if reason: enqueue_telegram(f"PAPER {config.SYMBOL} {d} {reason}\nRealized PnL: {realized:.2f} USD (Net: {weighted_r:+.2f}R)")
        c.commit()
    except Exception: c.rollback()
    finally: c.close()
