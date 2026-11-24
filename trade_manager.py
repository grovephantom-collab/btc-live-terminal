import time, hmac, hashlib, requests, logging, json
import database, config, notifications, market_data
from datetime import datetime, timezone

log = logging.getLogger("TradeManager")
HTTP = requests.Session()
DELTA_BASE_URL = "https://testnet-api.delta.exchange" if config.DELTA_TESTNET else "https://api.delta.exchange"
_PRODUCT_ID_CACHE = {}
_WALLET_CACHE = {"ts": 0.0, "balance": None, "available": None}

def _get_delta_signature(secret: str, method: str, path: str, payload_str: str, timestamp: str) -> str:
    return hmac.new(secret.encode("utf-8"), (method + timestamp + path + payload_str).encode("utf-8"), hashlib.sha256).hexdigest()

def get_delta_product_id(delta_symbol: str) -> int:
    sym = delta_symbol.strip().upper()
    if sym in _PRODUCT_ID_CACHE:
        return _PRODUCT_ID_CACHE[sym]
    try:
        r = HTTP.get(f"{DELTA_BASE_URL}/v2/products", timeout=6)
        if r.status_code == 200:
            for p in r.json().get("result", []):
                if p.get("symbol"):
                    _PRODUCT_ID_CACHE[str(p["symbol"]).upper()] = int(p["id"])
            if sym in _PRODUCT_ID_CACHE:
                return _PRODUCT_ID_CACHE[sym]
    except Exception:
        pass
    return config.DELTA_PRODUCT_ID

def fetch_delta_wallet_balance(force: bool = False) -> dict:
    if config.MODE == "PAPER" or not config.DELTA_API_KEY or not config.DELTA_API_SECRET:
        return {"balance": None, "available": None}
    now = time.time()
    if not force and (now - _WALLET_CACHE["ts"]) < 20.0 and _WALLET_CACHE["balance"] is not None:
        return {"balance": _WALLET_CACHE["balance"], "available": _WALLET_CACHE["available"]}
    path, ts = "/v2/wallet/balances", str(int(now))
    headers = {"api-key": config.DELTA_API_KEY, "signature": _get_delta_signature(config.DELTA_API_SECRET, "GET", path, "", ts), "timestamp": ts}
    try:
        r = HTTP.get(f"{DELTA_BASE_URL}{path}", headers=headers, timeout=6)
        if r.status_code == 200:
            tb, ab = 0.0, 0.0
            for w in r.json().get("result", []):
                if str(w.get("asset_symbol", "")).upper() in ("USD", "USDT"):
                    tb += float(w.get("balance", 0.0) or 0.0)
                    ab += float(w.get("available_balance", 0.0) or 0.0)
            _WALLET_CACHE.update(ts=now, balance=round(tb, 2), available=round(ab, 2))
    except Exception:
        pass
    return {"balance": _WALLET_CACHE["balance"], "available": _WALLET_CACHE["available"]}

def place_delta_order(symbol: str, side: str, lots: int, stop_loss: float, take_profit: float):
    if config.MODE == "PAPER" or not config.DELTA_API_KEY or not config.DELTA_API_SECRET:
        return None
    params = config.get_asset_params(symbol, stop_loss)
    pid, dec = get_delta_product_id(params["delta_symbol"]), int(params.get("decimals", 2))
    path, ts = "/v2/orders", str(int(time.time()))
    payload_str = json.dumps({
        "product_id": pid, "size": int(max(1, lots)), "side": side.lower(), "order_type": "market_order",
        "bracket_stop_loss_price": str(round(stop_loss, dec)), "bracket_take_profit_price": str(round(take_profit, dec)),
        "bracket_stop_loss_limit_price": str(round(stop_loss, dec)), "bracket_take_profit_limit_price": str(round(take_profit, dec)),
    }, separators=(",", ":"))
    headers = {"api-key": config.DELTA_API_KEY, "signature": _get_delta_signature(config.DELTA_API_SECRET, "POST", path, payload_str, ts), "timestamp": ts, "Content-Type": "application/json"}
    try:
        r = HTTP.post(f"{DELTA_BASE_URL}{path}", headers=headers, data=payload_str, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
    except Exception:
        pass
    return None

def get_base_capital(conn = None) -> float:
    delta_w = fetch_delta_wallet_balance()
    if delta_w.get("balance") is not None and float(delta_w["balance"]) > 0:
        return round(float(delta_w["balance"]), 2)
    try:
        added_usd = float(database.get_setting("added_capital_usd", "0.0") or 0.0)
    except Exception:
        added_usd = 0.0
    return round(float(config.START_EQUITY) + max(0.0, added_usd), 2)

def get_15d_compounding_state(conn) -> dict:
    now_dt = datetime.now(timezone.utc)
    base_cap = get_base_capital(conn)
    rows = conn.execute("SELECT realized_pnl_usd, total_fee_usd, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND closed_at_utc IS NOT NULL ORDER BY id ASC").fetchall()
    active_partial_pnl = float(conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as p FROM trades WHERE stage != 'CLOSED'").fetchone()["p"] or 0.0)
    active_partial_fee = float(conn.execute("SELECT COALESCE(SUM(total_fee_usd), 0.0) as f FROM trades WHERE stage != 'CLOSED'").fetchone()["f"] or 0.0)

    total_net = sum(float(r["realized_pnl_usd"] or 0.0) for r in rows) + active_partial_pnl
    total_fee = sum(float(r["total_fee_usd"] or 0.0) for r in rows) + active_partial_fee
    cycle_days = int(getattr(config, "CYCLE_DAYS", 15))
    cutoff_ts = now_dt.timestamp() - (cycle_days * 86400)
    c_pnl, c_fee, c_cnt = active_partial_pnl, active_partial_fee, 0
    for r in rows:
        try:
            if datetime.fromisoformat(str(r["closed_at_utc"])).timestamp() >= cutoff_ts:
                c_pnl += float(r["realized_pnl_usd"] or 0.0)
                c_fee += float(r["total_fee_usd"] or 0.0)
                c_cnt += 1
        except Exception:
            pass
    if total_net > 0:
        w_usd = round(total_net * float(getattr(config, "WITHDRAWAL_RESERVE_PCT", 0.70)), 2)
        r_usd = round(total_net * float(getattr(config, "COMPOUND_REINVEST_PCT", 0.30)), 2)
        act_eq = round(base_cap + r_usd, 2)
    else:
        w_usd, r_usd, act_eq = 0.0, round(total_net, 2), round(max(6.0, base_cap + total_net), 2)

    capital_scale = act_eq / 58.0
    lot_mult = round(max(0.75, min(10.0, capital_scale)), 2)
    return {
        "base_capital_usd": base_cap, "total_gross_pnl_usd": round(total_net + total_fee, 2),
        "total_brokerage_usd": round(total_fee, 2), "total_net_pnl_usd": round(total_net, 2), "cycle_days": cycle_days,
        "cycle_15d_net_pnl_usd": round(c_pnl, 2), "cycle_15d_fees_usd": round(c_fee, 2), "cycle_15d_trades": c_cnt,
        "withdrawable_usd": w_usd, "reinvested_usd": r_usd, "active_trading_equity_usd": act_eq,
        "total_portfolio_equity_usd": round(base_cap + total_net, 2), "lot_multiplier": lot_mult,
        "scaling_status": f"CAPITAL SCALED ({lot_mult:.2f}x)" if base_cap > 58.0 else ("SCALING UP (PROFIT)" if c_pnl > 0 else ("RECOVERY MODE (TIGHT SL)" if c_pnl < 0 else "BASE LOT SIZE")),
    }

def get_equity(conn) -> float:
    return get_15d_compounding_state(conn)["active_trading_equity_usd"]

def _get_today_realized_pnl(conn) -> float:
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
    closed_p = float(conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as s FROM trades WHERE closed_at_utc >= ?", (today_start,)).fetchone()["s"] or 0.0)
    active_p = float(conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as s FROM trades WHERE stage != 'CLOSED' AND opened_at_utc >= ?", (today_start,)).fetchone()["s"] or 0.0)
    return round(closed_p + active_p, 2)

def _check_cooldown_and_circuit_breakers(conn, symbol: str = None) -> tuple:
    now_dt = datetime.now(timezone.utc)
    today_start = now_dt.strftime("%Y-%m-%d 00:00:00")
    if conn.execute("SELECT COUNT(*) as c FROM trades WHERE opened_at_utc >= ?", (today_start,)).fetchone()["c"] >= config.MAX_DAILY_TRADES:
        return False, "Daily trade limit"
    cap_mult = max(1.0, get_base_capital(conn) / 58.0)
    day_pnl = _get_today_realized_pnl(conn)
    if day_pnl <= -abs(config.MAX_DAILY_LOSS * cap_mult):
        return False, "Daily loss circuit breaker"
    # Global 2-Consecutive-SL Chop Guard: pause 2 hours if last 2 closed trades across ANY coin hit SL!
    last2 = conn.execute("SELECT close_reason, realized_pnl_usd, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND closed_at_utc IS NOT NULL ORDER BY id DESC LIMIT 2").fetchall()
    if len(last2) == 2 and all(float(r["realized_pnl_usd"] or 0.0) < 0 for r in last2):
        try:
            since_last_sl = (now_dt - datetime.fromisoformat(last2[0]["closed_at_utc"])).total_seconds()
            if since_last_sl < getattr(config, "CONSECUTIVE_SL_PAUSE_SEC", 7200):
                return False, "2-SL chop guard pause"
        except Exception:
            pass
    # Daily Profit-Floor Lock: once today hits +$10 target, never let realized profit drop below +$8.00!
    today_key = now_dt.strftime("%Y-%m-%d")
    daily_target = float(getattr(config, "DAILY_PROFIT_TARGET", 10.0)) * cap_mult
    if day_pnl >= daily_target:
        database.set_setting("daily_target_hit_date", today_key)
    if database.get_setting("daily_target_hit_date", "") == today_key and day_pnl < (daily_target * 0.80):
        return False, "Daily profit floor locked"
    recent = conn.execute("SELECT close_reason, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND symbol = ? AND closed_at_utc IS NOT NULL ORDER BY id DESC LIMIT 2", (symbol,)).fetchall() if symbol else []
    if recent and recent[0]["closed_at_utc"]:
        try:
            elapsed = (now_dt - datetime.fromisoformat(recent[0]["closed_at_utc"])).total_seconds()
            reason = recent[0]["close_reason"] or ""
            if ("STOP_LOSS" in reason or "INVALIDATION" in reason) and elapsed < getattr(config, "SL_COOLDOWN_SECONDS", 5400):
                return False, "SL cooldown"
            if elapsed < getattr(config, "TRADE_COOLDOWN_SECONDS", 1200):
                return False, "Rotation cooldown"
        except Exception:
            pass
    return True, "OK"

def calculate_small_cap_delta_size(equity_usd: float, entry: float, sl: float, symbol: str = "BTCUSDT", score: float = 95.0, profit_lock_mode: bool = False) -> tuple:
    """
    Stronger Uniform USD Risk Sizing ($2.30-$2.90 on $58) for $10/day target:
    - Scales proportionally with equity & added capital
    - Automatically halves risk in Profit-Lock Mode once Daily +$10 target is achieved!
    """
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0 or entry <= 0:
        return 0.0, 0, 0.0
    params = config.get_asset_params(symbol, entry)
    lot_size = float(params["lot_size"])
    capital_mult = max(0.75, equity_usd / 58.0)
    risk_pct = float(getattr(config, "A_PLUS_RISK_PCT", 0.050)) if score >= 97.5 else float(getattr(config, "RISK_PCT", 0.040))
    if profit_lock_mode:
        risk_pct *= 0.50
    min_r = float(config.MIN_RISK_USD) * capital_mult * (0.50 if profit_lock_mode else 1.0)
    max_r = float(config.MAX_RISK_USD) * capital_mult * (0.50 if profit_lock_mode else 1.0)
    target_risk = max(min_r, min(max_r, equity_usd * risk_pct))

    # BROKERAGE FEE-GUARD: Never allow round-trip Delta fee+GST to exceed 15% of trade risk!
    fee_rate = max(0.0001, float(getattr(config, "FEE_RATE", 0.00059)))
    max_notional_fee_safe = (target_risk * 0.15) / (2.0 * fee_rate)
    max_notional_usd = min(float(getattr(config, "MAX_NOTIONAL", 850.0)) * capital_mult, equity_usd * float(config.LEVERAGE) * 0.58, max_notional_fee_safe)
    max_lots_notional = max(1, int((max_notional_usd / entry) / lot_size))
    dyn_max_lots = min(max(int(getattr(config, "MAX_LOTS_CAP", 600)), int(round(600 * capital_mult))), max_lots_notional)

    raw_lots = int(round((target_risk / risk_per_unit) / lot_size))
    lots = max(1, min(dyn_max_lots, raw_lots))
    qty = round(lots * lot_size, 4)
    actual_risk = round(qty * risk_per_unit, 2)
    if actual_risk < round(target_risk * 0.60, 2):
        return 0.0, 0, 0.0
    return qty, lots, actual_risk

def open_paper_trade(signal: dict, fencing_token: int):
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token) or database.get_setting("auto_trade_enabled", "1") != "1":
            return
        sym = signal.get("symbol", config.SYMBOL)
        if conn.execute("SELECT id FROM trades WHERE stage != 'CLOSED' AND symbol = ?", (sym,)).fetchone():
            return
        active_rows = conn.execute("SELECT symbol, direction, entry_price, stop_price, tp1_done FROM trades WHERE stage != 'CLOSED'").fetchall()
        if len(active_rows) >= getattr(config, "MAX_OPEN_TRADES", 3):
            return

        if not any(g in sym.upper() for g in ("PAXG", "XAU", "GOLD")):
            unlocked_same_dir = 0
            for r in active_rows:
                r_sym = str(r["symbol"]).upper()
                if any(g in r_sym for g in ("PAXG", "XAU", "GOLD")):
                    continue
                if r["direction"] == signal["direction"]:
                    r_entry, r_sl = float(r["entry_price"] or 0), float(r["stop_price"] or 0)
                    r_locked = bool(r["tp1_done"]) or (r["direction"] == "LONG" and r_sl >= r_entry) or (r["direction"] == "SHORT" and r_sl <= r_entry)
                    if not r_locked:
                        unlocked_same_dir += 1
            if unlocked_same_dir >= 1:
                return

        allowed, _ = _check_cooldown_and_circuit_breakers(conn, sym)
        if not allowed:
            return
        entry, sl = float(signal["entry"]), float(signal["sl"])
        if abs(entry - sl) <= 0:
            return
        candle_ts = int(signal.get("candle_time") or (int(time.time()) // 900) * 900)
        sig_key = f"{sym}_{signal['direction']}_{candle_ts}"
        if conn.execute("SELECT id FROM trades WHERE signal_key = ?", (sig_key,)).fetchone():
            return
        equity = get_equity(conn)
        cap_mult = max(1.0, get_base_capital(conn) / 58.0)
        daily_target = float(getattr(config, "DAILY_PROFIT_TARGET", 10.0)) * cap_mult
        profit_lock_mode = _get_today_realized_pnl(conn) >= daily_target
        if profit_lock_mode and float(signal.get("score") or 0.0) < 98.0:
            return

        qty, lots, risk_usd = calculate_small_cap_delta_size(equity, entry, sl, sym, float(signal.get("score") or 95.0), profit_lock_mode)
        if qty <= 0 or lots <= 0:
            return
        params = config.get_asset_params(sym, entry)
        dec = int(params.get("decimals", 2))
        now_str = database.now_iso()
        conn.execute(
            "INSERT INTO trades (trade_id, signal_key, symbol, direction, setup_name, score, entry_price, stop_price, tp1_price, tp2_price, tp3_price, initial_qty, remaining_qty, stage, opened_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (f"TRD-{int(time.time()*1000)}", sig_key, sym, signal["direction"], signal["setup_name"], signal["score"], entry, sl, signal["tp1"], signal["tp2"], signal["tp3"], qty, qty, now_str, now_str),
        )
        conn.commit()
        if hasattr(notifications, "enqueue_telegram"):
            is_ev = "Event Level" in str(signal.get("setup_name") or "")
            tp_lbl = ("1.50R", "2.80R", "4.50R") if is_ev else ("1.25R", "2.30R", "3.50R")
            mode_tag = " [PROFIT-LOCK 50% RISK]" if profit_lock_mode else ""
            ev_icon = "⚡" if is_ev else "🚀"
            notifications.enqueue_telegram(
                f"{ev_icon} *HIGH-CONVICTION TRADE OPENED ({params['display_name']} {signal['direction']}){mode_tag}*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Setup: {signal['setup_name']}\n"
                f"• Entry: ${entry:,.{dec}f} | SL: ${sl:,.{dec}f} ({round(abs(entry-sl), dec)} pts)\n"
                f"• TP1 ({tp_lbl[0]}): ${signal['tp1']:,.{dec}f} | TP2 ({tp_lbl[1]}): ${signal['tp2']:,.{dec}f} | Runner ({tp_lbl[2]}): ${signal['tp3']:,.{dec}f}\n"
                f"• Size: {lots} Lot ({qty})\n"
                f"• Max Risk: ${risk_usd:.2f} (~₹{risk_usd*config.USD_INR_RATE:.0f})\n"
                f"• Active Equity: ${equity:.2f} USD"
            )
        if config.MODE in ("TESTNET", "LIVE"):
            place_delta_order(sym, "buy" if signal["direction"] == "LONG" else "sell", lots, sl, signal["tp2"])
    finally:
        conn.close()

def _manage_single_position(conn, trade_dict: dict, current_price: float, fencing_token: int):
    direction, entry, sl = trade_dict["direction"], float(trade_dict["entry_price"]), float(trade_dict["stop_price"])
    tp1, tp2, tp3 = float(trade_dict["tp1_price"]), float(trade_dict["tp2_price"]), float(trade_dict["tp3_price"])
    rem_qty, init_qty = float(trade_dict["remaining_qty"]), float(trade_dict["initial_qty"])
    tp1_done, tp2_done = bool(trade_dict["tp1_done"]), bool(trade_dict["tp2_done"])
    tid, sym, now_str = trade_dict["trade_id"], trade_dict.get("symbol") or "BTCUSDT", database.now_iso()
    is_long = direction == "LONG"
    params = config.get_asset_params(sym, entry)
    dec = int(params.get("decimals", 2))
    is_event = "Event Level" in str(trade_dict.get("setup_name") or "")
    tp1_r, tp2_r = (1.50, 2.80) if is_event else (1.25, 2.30)
    orig_sl_dist = max(abs(tp1 - entry) / tp1_r, params["min_stop"])

    if (is_long and current_price <= sl) or (not is_long and current_price >= sl):
        exec_exit = sl if config.MODE == "PAPER" else current_price
        fee_usd = round((entry + exec_exit) * rem_qty * float(config.FEE_RATE), 3)
        leg_pnl = round((((exec_exit - entry) if is_long else (entry - exec_exit)) * rem_qty) - fee_usd, 2)
        tot_real = round(float(trade_dict["realized_pnl_usd"]) + leg_pnl, 2)
        in_prof = (is_long and sl >= entry) or (not is_long and sl <= entry)
        reason = "BREAK_EVEN_EXIT" if (tp1_done or in_prof) else "STOP_LOSS_HIT"
        conn.execute("UPDATE trades SET stage='CLOSED', remaining_qty=0, realized_pnl_usd=?, unrealized_pnl_usd=0, total_fee_usd=total_fee_usd+?, close_reason=?, closed_at_utc=?, updated_at_utc=? WHERE trade_id=?", (tot_real, fee_usd, reason, now_str, now_str, tid))
        conn.commit()
        title = "🛡️ BREAK-EVEN SL HIT (PROFIT SECURED)" if (tp1_done or in_prof) else "🛑 STOP LOSS HIT"
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"{title} ({params['display_name']} {direction})\n━━━━━━━━━━━━━━━━━━━━\n• Exit Price: ${exec_exit:,.{dec}f}\n• Net PnL: ${tot_real:+.2f} USD\n• Reason: {reason}")
        return

    # EARLY STALLED LOSER CUT: If stalled >90 min with zero TP1 and losing >= 0.55R, exit early & save ~45% SL
    if not tp1_done and trade_dict.get("opened_at_utc"):
        try:
            elapsed_sec = (datetime.fromisoformat(now_str) - datetime.fromisoformat(trade_dict["opened_at_utc"])).total_seconds()
            adverse_dist = (entry - current_price) if is_long else (current_price - entry)
            m_live = market_data.get_market(sym)
            live_cvd = float(m_live.get("cvd_ratio") or 0.50)
            cvd_flipped_against = (is_long and live_cvd <= 0.40) or ((not is_long) and live_cvd >= 0.60)
            if (elapsed_sec >= 5400 and adverse_dist >= (orig_sl_dist * 0.55)) or (elapsed_sec >= 1800 and cvd_flipped_against and adverse_dist >= (orig_sl_dist * 0.55)):
                fee_usd = round((entry + current_price) * rem_qty * float(config.FEE_RATE), 3)
                leg_pnl = round((-adverse_dist * rem_qty) - fee_usd, 2)
                tot_real = round(float(trade_dict["realized_pnl_usd"]) + leg_pnl, 2)
                conn.execute("UPDATE trades SET stage='CLOSED', remaining_qty=0, realized_pnl_usd=?, unrealized_pnl_usd=0, total_fee_usd=total_fee_usd+?, close_reason='STRUCTURAL_INVALIDATION_EXIT', closed_at_utc=?, updated_at_utc=? WHERE trade_id=?", (tot_real, fee_usd, now_str, now_str, tid))
                conn.commit()
                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(f"⚠️ *EARLY LOSS CUT ({params['display_name']} {direction})*\n━━━━━━━━━━━━━━━━━━━━\n• Exit Price: ${current_price:,.{dec}f}\n• Saved ~45% SL: ${tot_real:+.2f} USD")
                return
        except Exception:
            pass

    current_gain = (current_price - entry) if is_long else (entry - current_price)
    r_mult = current_gain / max(1e-9, orig_sl_dist)

    # TP1 AT +1.25R (or +1.50R for Event Level): Book 50% CASH PROFIT & lock remaining 50% risk-free at Break-Even + Fees!
    tp1_reached = (is_long and current_price >= tp1) or (not is_long and current_price <= tp1) or (r_mult >= tp1_r)
    if not tp1_done and tp1_reached:
        exec_tp1 = current_price if r_mult >= tp1_r else tp1
        b_qty = round(init_qty * 0.50, 6)
        n_rem = max(0.0, round(rem_qty - b_qty, 6))
        fee_usd = round((entry + exec_tp1) * b_qty * float(config.FEE_RATE), 3)
        gain_pts = (exec_tp1 - entry) if is_long else (entry - exec_tp1)
        b_pnl = round((gain_pts * b_qty) - fee_usd, 2)
        fee_dist = entry * float(config.FEE_RATE) * 2.5
        be_off = max(float(params.get("be_buffer", 0.01)), fee_dist + (orig_sl_dist * 0.15))
        n_sl = round(entry + be_off if is_long else entry - be_off, dec)
        rem_unreal = round(gain_pts * n_rem, 2)
        conn.execute("UPDATE trades SET tp1_done=1, remaining_qty=?, stop_price=?, realized_pnl_usd=realized_pnl_usd+?, unrealized_pnl_usd=?, total_fee_usd=total_fee_usd+?, updated_at_utc=? WHERE trade_id=?", (n_rem, n_sl, b_pnl, rem_unreal, fee_usd, now_str, tid))
        conn.commit()
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"🎯 *TARGET 1 BOOKED 50% ({params['display_name']})*\n━━━━━━━━━━━━━━━━━━━━\n• Booked 50% at: ${exec_tp1:,.{dec}f}\n• Realized Profit Added: ${b_pnl:+.2f} USD (~₹{b_pnl*config.USD_INR_RATE:+.0f})\n• Remaining 50% Risk-Free SL: ${n_sl:,.{dec}f}")
        return

    # BREATHING-ROOM RISK SLASH: At +0.75R, slash risk by 50%
    if getattr(config, "ENABLE_DYNAMIC_PROFIT_LOCK", True) and not tp1_done and r_mult >= 0.75:
        t_sl = round(entry - orig_sl_dist * 0.50 if is_long else entry + orig_sl_dist * 0.50, dec)
        if (is_long and t_sl > sl) or (not is_long and t_sl < sl):
            conn.execute("UPDATE trades SET stop_price=?, updated_at_utc=? WHERE trade_id=?", (t_sl, now_str, tid))
            conn.commit()
            sl = t_sl

    # MID-RUNNER TRAIL: After TP1, at +1.75R (or +2.10R for Event Level) lock SL at +0.90R (+1.10R) before TP2
    mid_trig, mid_lock = (2.10, 1.10) if is_event else (1.75, 0.90)
    if tp1_done and not tp2_done and r_mult >= mid_trig:
        mid_sl = round(entry + (orig_sl_dist * mid_lock) if is_long else entry - (orig_sl_dist * mid_lock), dec)
        if (is_long and mid_sl > sl) or (not is_long and mid_sl < sl):
            conn.execute("UPDATE trades SET stop_price=?, updated_at_utc=? WHERE trade_id=?", (mid_sl, now_str, tid))
            conn.commit()
            sl = mid_sl

    # TP2 AT +2.30R (or +2.80R for Event Level): Book 30% more and trail SL to +1.45R (+1.80R)
    tp2_reached = (is_long and current_price >= tp2) or (not is_long and current_price <= tp2) or (r_mult >= tp2_r)
    if tp1_done and not tp2_done and tp2_reached:
        exec_tp2 = current_price if r_mult >= tp2_r else tp2
        b_qty = round(init_qty * 0.30, 6)
        n_rem = max(0.0, round(rem_qty - b_qty, 6))
        fee_usd = round((entry + exec_tp2) * b_qty * float(config.FEE_RATE), 3)
        gain_pts = (exec_tp2 - entry) if is_long else (entry - exec_tp2)
        b_pnl = round((gain_pts * b_qty) - fee_usd, 2)
        tp2_lock = 1.80 if is_event else 1.45
        n_sl = round(entry + (orig_sl_dist * tp2_lock) if is_long else entry - (orig_sl_dist * tp2_lock), dec)
        rem_unreal = round(gain_pts * n_rem, 2)
        conn.execute("UPDATE trades SET tp2_done=1, remaining_qty=?, stop_price=?, realized_pnl_usd=realized_pnl_usd+?, unrealized_pnl_usd=?, total_fee_usd=total_fee_usd+?, updated_at_utc=? WHERE trade_id=?", (n_rem, n_sl, b_pnl, rem_unreal, fee_usd, now_str, tid))
        conn.commit()
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"🎯 *TARGET 2 BOOKED 30% ({params['display_name']})*\n━━━━━━━━━━━━━━━━━━━━\n• Booked 30% at: ${exec_tp2:,.{dec}f}\n• Realized Profit Added: ${b_pnl:+.2f} USD\n• Final 20% Runner SL Trailed to +{tp2_lock:.2f}R: ${n_sl:,.{dec}f}")
        return

    # RUNNER PROFIT RATCHET: At +2.90R (+3.50R for Event) trail SL to +2.20R (+2.60R)
    run_trig, run_lock = (3.50, 2.60) if is_event else (2.90, 2.20)
    if tp1_done and tp2_done and r_mult >= run_trig:
        r_sl = round(entry + (orig_sl_dist * run_lock) if is_long else entry - (orig_sl_dist * run_lock), dec)
        if (is_long and r_sl > sl) or (not is_long and r_sl < sl):
            conn.execute("UPDATE trades SET stop_price=?, updated_at_utc=? WHERE trade_id=?", (r_sl, now_str, tid))
            conn.commit()
            sl = r_sl

    if tp1_done and tp2_done and ((is_long and current_price >= tp3) or (not is_long and current_price <= tp3)):
        fee_usd = round((entry + tp3) * rem_qty * float(config.FEE_RATE), 3)
        f_pnl = round((((tp3 - entry) if is_long else (entry - tp3)) * rem_qty) - fee_usd, 2)
        tot_real = round(float(trade_dict["realized_pnl_usd"]) + f_pnl, 2)
        conn.execute("UPDATE trades SET stage='CLOSED', remaining_qty=0, realized_pnl_usd=?, unrealized_pnl_usd=0, total_fee_usd=total_fee_usd+?, close_reason='TP3_RUNNER_HIT', closed_at_utc=?, updated_at_utc=? WHERE trade_id=?", (tot_real, fee_usd, now_str, now_str, tid))
        conn.commit()
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"🏆 *TARGET 3 MEGA-RUNNER HIT ({params['display_name']})*\n━━━━━━━━━━━━━━━━━━━━\n• Final Target: ${tp3:,.{dec}f}\n• Total Net PnL: ${tot_real:+.2f} USD (~₹{tot_real*config.USD_INR_RATE:+.0f})")
        return

    cur_pnl = round(current_gain * rem_qty, 2)
    conn.execute("UPDATE trades SET unrealized_pnl_usd=?, updated_at_utc=? WHERE trade_id=?", (cur_pnl, now_str, tid))
    conn.commit()

def manage_all_positions(fencing_token: int, lock):
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token):
            return
        for row in conn.execute("SELECT * FROM trades WHERE stage != 'CLOSED'").fetchall():
            td = dict(row)
            sym = td.get("symbol") or "BTCUSDT"
            with lock:
                cur_p = market_data.get_market(sym).get("price")
            cur_p = cur_p or market_data.fetch_price(sym)
            if cur_p and cur_p > 0:
                _manage_single_position(conn, td, cur_p, fencing_token)
    finally:
        conn.close()
