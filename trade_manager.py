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
        w_usd = round(total_net * float(getattr(config, "WITHDRAWAL_RESERVE_PCT", 0.30)), 2)
        r_usd = round(total_net * float(getattr(config, "COMPOUND_REINVEST_PCT", 0.70)), 2)
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

def _check_cooldown_and_circuit_breakers(conn, symbol: str = None) -> tuple:
    now_dt = datetime.now(timezone.utc)
    today_start = now_dt.strftime("%Y-%m-%d 00:00:00")
    if conn.execute("SELECT COUNT(*) as c FROM trades WHERE opened_at_utc >= ?", (today_start,)).fetchone()["c"] >= config.MAX_DAILY_TRADES:
        return False, "Daily trade limit"
    cap_mult = max(1.0, get_base_capital(conn) / 58.0)
    if float(conn.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as s FROM trades WHERE closed_at_utc >= ?", (today_start,)).fetchone()["s"] or 0.0) <= -abs(config.MAX_DAILY_LOSS * cap_mult):
        return False, "Daily loss circuit breaker"
    recent = conn.execute("SELECT close_reason, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND symbol = ? AND closed_at_utc IS NOT NULL ORDER BY id DESC LIMIT 2", (symbol,)).fetchall() if symbol else []
    if recent and recent[0]["closed_at_utc"]:
        try:
            elapsed = (now_dt - datetime.fromisoformat(recent[0]["closed_at_utc"])).total_seconds()
            reason = recent[0]["close_reason"] or ""
            if "STOP_LOSS" in reason and elapsed < getattr(config, "SL_COOLDOWN_SECONDS", 3600):
                return False, "SL cooldown"
            if elapsed < getattr(config, "TRADE_COOLDOWN_SECONDS", 900):
                return False, "Trade cooldown"
        except Exception:
            pass
    return True, "OK"

def calculate_small_cap_delta_size(equity_usd: float, entry: float, sl: float, symbol: str = "BTCUSDT", score: float = 95.0) -> tuple:
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0, 0, 0.0
    params = config.get_asset_params(symbol, entry)
    lot_size = params["lot_size"]
    capital_mult = max(0.75, equity_usd / 58.0)
    risk_pct = float(getattr(config, "A_PLUS_RISK_PCT", 0.024)) if score >= 97.0 else float(getattr(config, "RISK_PCT", 0.018))
    min_r = float(config.MIN_RISK_USD) * capital_mult
    max_r = float(config.MAX_RISK_USD) * capital_mult
    target_risk = max(min_r, min(max_r, equity_usd * risk_pct))
    dyn_max_lots = max(int(getattr(config, "MAX_LOTS_CAP", 15)), int(round(15 * capital_mult)))
    lots = max(1, min(dyn_max_lots, int(round((target_risk / risk_per_unit) / lot_size))))
    qty = round(lots * lot_size, 4)
    return qty, lots, round(qty * risk_per_unit, 2)

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
            for r in active_rows:
                r_sym = str(r["symbol"]).upper()
                if any(g in r_sym for g in ("PAXG", "XAU", "GOLD")):
                    continue
                if r["direction"] == signal["direction"]:
                    r_entry, r_sl = float(r["entry_price"] or 0), float(r["stop_price"] or 0)
                    r_locked = bool(r["tp1_done"]) or (r["direction"] == "LONG" and r_sl >= r_entry) or (r["direction"] == "SHORT" and r_sl <= r_entry)
                    if not r_locked:
                        log.info("Blocked correlated %s %s until existing %s locks profit.", sym, signal["direction"], r_sym)
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
        qty, lots, risk_usd = calculate_small_cap_delta_size(equity, entry, sl, sym, float(signal.get("score") or 95.0))
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
            notifications.enqueue_telegram(
                f"🚀 *HIGH-CONVICTION TRADE OPENED ({params['display_name']} {signal['direction']})*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Setup: {signal['setup_name']}\n"
                f"• Entry: ${entry:,.{dec}f} | SL: ${sl:,.{dec}f} ({round(abs(entry-sl), dec)} pts)\n"
                f"• TP1 (0.9R): ${signal['tp1']:,.{dec}f} | TP2 (1.75R): ${signal['tp2']:,.{dec}f} | Runner: ${signal['tp3']:,.{dec}f}\n"
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
    orig_sl_dist = max(abs(tp1 - entry) / 0.90, params["min_stop"])

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

    if not tp1_done and trade_dict.get("opened_at_utc"):
        try:
            elapsed_sec = (datetime.fromisoformat(now_str) - datetime.fromisoformat(trade_dict["opened_at_utc"])).total_seconds()
            adverse_dist = (entry - current_price) if is_long else (current_price - entry)
            if elapsed_sec >= 5400 and adverse_dist >= (orig_sl_dist * 0.58):
                fee_usd = round((entry + current_price) * rem_qty * float(config.FEE_RATE), 3)
                leg_pnl = round((-adverse_dist * rem_qty) - fee_usd, 2)
                tot_real = round(float(trade_dict["realized_pnl_usd"]) + leg_pnl, 2)
                conn.execute("UPDATE trades SET stage='CLOSED', remaining_qty=0, realized_pnl_usd=?, unrealized_pnl_usd=0, total_fee_usd=total_fee_usd+?, close_reason='STRUCTURAL_INVALIDATION_EXIT', closed_at_utc=?, updated_at_utc=? WHERE trade_id=?", (tot_real, fee_usd, now_str, now_str, tid))
                conn.commit()
                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(f"⚠️ *SMART LOSS CUT ({params['display_name']} {direction})*\n━━━━━━━━━━━━━━━━━━━━\n• Exit Price: ${current_price:,.{dec}f}\n• Saved Half SL: ${tot_real:+.2f} USD")
                return
        except Exception:
            pass

    current_gain = (current_price - entry) if is_long else (entry - current_price)
    r_mult = current_gain / max(1e-9, orig_sl_dist)

    tp1_reached = (is_long and current_price >= tp1) or (not is_long and current_price <= tp1) or (r_mult >= 0.85)
    if not tp1_done and tp1_reached:
        exec_tp1 = current_price if r_mult >= 0.85 else tp1
        b_qty = round(init_qty * 0.50, 6)
        n_rem = max(0.0, round(rem_qty - b_qty, 6))
        fee_usd = round((entry + exec_tp1) * b_qty * float(config.FEE_RATE), 3)
        gain_pts = (exec_tp1 - entry) if is_long else (entry - exec_tp1)
        b_pnl = round((gain_pts * b_qty) - fee_usd, 2)
        fee_dist = entry * float(config.FEE_RATE) * 2.5
        be_off = max(float(params.get("be_buffer", 0.01)), fee_dist + (orig_sl_dist * 0.18))
        n_sl = round(entry + be_off if is_long else entry - be_off, dec)
        rem_unreal = round(gain_pts * n_rem, 2)
        conn.execute("UPDATE trades SET tp1_done=1, remaining_qty=?, stop_price=?, realized_pnl_usd=realized_pnl_usd+?, unrealized_pnl_usd=?, total_fee_usd=total_fee_usd+?, updated_at_utc=? WHERE trade_id=?", (n_rem, n_sl, b_pnl, rem_unreal, fee_usd, now_str, tid))
        conn.commit()
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"🎯 *TARGET 1 CASH BOOKED ({params['display_name']})*\n━━━━━━━━━━━━━━━━━━━━\n• Booked 50% at: ${exec_tp1:,.{dec}f}\n• Realized Profit Added: ${b_pnl:+.2f} USD (~₹{b_pnl*config.USD_INR_RATE:+.0f})\n• SL Trailed to Profit Lock: ${n_sl:,.{dec}f}")
        return

    if getattr(config, "ENABLE_DYNAMIC_PROFIT_LOCK", True) and not tp1_done and r_mult >= 0.55:
        t_sl = round(entry - orig_sl_dist * 0.40 if is_long else entry + orig_sl_dist * 0.40, dec)
        if (is_long and t_sl > sl) or (not is_long and t_sl < sl):
            conn.execute("UPDATE trades SET stop_price=?, updated_at_utc=? WHERE trade_id=?", (t_sl, now_str, tid))
            conn.commit()

    tp2_reached = (is_long and current_price >= tp2) or (not is_long and current_price <= tp2) or (r_mult >= 1.65)
    if tp1_done and not tp2_done and tp2_reached:
        exec_tp2 = current_price if r_mult >= 1.65 else tp2
        b_qty = round(init_qty * 0.30, 6)
        n_rem = max(0.0, round(rem_qty - b_qty, 6))
        fee_usd = round((entry + exec_tp2) * b_qty * float(config.FEE_RATE), 3)
        gain_pts = (exec_tp2 - entry) if is_long else (entry - exec_tp2)
        b_pnl = round((gain_pts * b_qty) - fee_usd, 2)
        n_sl = round(entry + (orig_sl_dist * 0.75) if is_long else entry - (orig_sl_dist * 0.75), dec)
        rem_unreal = round(gain_pts * n_rem, 2)
        conn.execute("UPDATE trades SET tp2_done=1, remaining_qty=?, stop_price=?, realized_pnl_usd=realized_pnl_usd+?, unrealized_pnl_usd=?, total_fee_usd=total_fee_usd+?, updated_at_utc=? WHERE trade_id=?", (n_rem, n_sl, b_pnl, rem_unreal, fee_usd, now_str, tid))
        conn.commit()
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"🎯 *TARGET 2 CASH BOOKED ({params['display_name']})*\n━━━━━━━━━━━━━━━━━━━━\n• Booked 30% at: ${exec_tp2:,.{dec}f}\n• Realized Profit Added: ${b_pnl:+.2f} USD\n• SL Trailed to +0.75R: ${n_sl:,.{dec}f}")
        return

    if tp1_done and tp2_done and ((is_long and current_price >= tp3) or (not is_long and current_price <= tp3)):
        fee_usd = round((entry + tp3) * rem_qty * float(config.FEE_RATE), 3)
        f_pnl = round((((tp3 - entry) if is_long else (entry - tp3)) * rem_qty) - fee_usd, 2)
        tot_real = round(float(trade_dict["realized_pnl_usd"]) + f_pnl, 2)
        conn.execute("UPDATE trades SET stage='CLOSED', remaining_qty=0, realized_pnl_usd=?, unrealized_pnl_usd=0, total_fee_usd=total_fee_usd+?, close_reason='TP3_RUNNER_HIT', closed_at_utc=?, updated_at_utc=? WHERE trade_id=?", (tot_real, fee_usd, now_str, now_str, tid))
        conn.commit()
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(f"🏆 *TARGET 3 FULL EXIT ({params['display_name']})*\n━━━━━━━━━━━━━━━━━━━━\n• Final Target: ${tp3:,.{dec}f}\n• Total Net PnL: ${tot_real:+.2f} USD")
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
