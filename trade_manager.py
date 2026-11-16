import time, uuid, hmac, hashlib, requests, logging, json
import database, config, notifications, market_data
from datetime import datetime, timezone

log = logging.getLogger("TradeManager")
HTTP = requests.Session()

DELTA_BASE_URL = "https://testnet-api.delta.exchange" if config.DELTA_TESTNET else "https://api.delta.exchange"
_PRODUCT_ID_CACHE = {}
_WALLET_CACHE = {"ts": 0.0, "balance": None, "available": None}

def _get_delta_signature(secret: str, method: str, path: str, payload_str: str, timestamp: str) -> str:
    message = method + timestamp + path + payload_str
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

def get_delta_product_id(delta_symbol: str) -> int:
    sym = delta_symbol.strip().upper()
    if sym in _PRODUCT_ID_CACHE:
        return _PRODUCT_ID_CACHE[sym]
    try:
        r = HTTP.get(f"{DELTA_BASE_URL}/v2/products", timeout=6)
        if r.status_code == 200:
            for p in r.json().get("result", []):
                p_sym = str(p.get("symbol", "")).upper()
                if p_sym:
                    _PRODUCT_ID_CACHE[p_sym] = int(p["id"])
            if sym in _PRODUCT_ID_CACHE:
                return _PRODUCT_ID_CACHE[sym]
    except Exception as e:
        log.warning("Delta product lookup failed for %s: %s", sym, e)
    return config.DELTA_PRODUCT_ID

def fetch_delta_wallet_balance(force: bool = False) -> dict:
    if config.MODE == "PAPER" or not config.DELTA_API_KEY or not config.DELTA_API_SECRET:
        return {"balance": None, "available": None}
    now = time.time()
    if not force and (now - _WALLET_CACHE["ts"]) < 20.0 and _WALLET_CACHE["balance"] is not None:
        return {"balance": _WALLET_CACHE["balance"], "available": _WALLET_CACHE["available"]}
    path = "/v2/wallet/balances"
    timestamp = str(int(now))
    sig = _get_delta_signature(config.DELTA_API_SECRET, "GET", path, "", timestamp)
    headers = {"api-key": config.DELTA_API_KEY, "signature": sig, "timestamp": timestamp}
    try:
        r = HTTP.get(f"{DELTA_BASE_URL}{path}", headers=headers, timeout=6)
        if r.status_code == 200:
            total_bal = 0.0
            avail_bal = 0.0
            for w in r.json().get("result", []):
                if str(w.get("asset_symbol", "")).upper() in ("USD", "USDT"):
                    total_bal += float(w.get("balance", 0.0) or 0.0)
                    avail_bal += float(w.get("available_balance", 0.0) or 0.0)
            _WALLET_CACHE.update(ts=now, balance=round(total_bal, 2), available=round(avail_bal, 2))
            return {"balance": _WALLET_CACHE["balance"], "available": _WALLET_CACHE["available"]}
    except Exception as e:
        log.warning("Delta wallet balance fetch error: %s", e)
    return {"balance": _WALLET_CACHE["balance"], "available": _WALLET_CACHE["available"]}

def _set_delta_leverage(product_id: int, leverage: int):
    if not config.DELTA_API_KEY or not config.DELTA_API_SECRET:
        return
    path = f"/v2/products/{product_id}/orders/leverage"
    timestamp = str(int(time.time()))
    payload_str = json.dumps({"leverage": str(int(leverage))}, separators=(",", ":"))
    sig = _get_delta_signature(config.DELTA_API_SECRET, "POST", path, payload_str, timestamp)
    headers = {"api-key": config.DELTA_API_KEY, "signature": sig, "timestamp": timestamp, "Content-Type": "application/json"}
    try:
        HTTP.post(f"{DELTA_BASE_URL}{path}", headers=headers, data=payload_str, timeout=5)
    except Exception:
        pass

def place_delta_order(symbol: str, side: str, lots: int, stop_loss: float, take_profit: float):
    if config.MODE == "PAPER":
        return None
    if not config.DELTA_API_KEY or not config.DELTA_API_SECRET:
        log.warning("Delta API credentials missing. Skipping exchange execution.")
        return None

    params = config.get_asset_params(symbol)
    product_id = get_delta_product_id(params["delta_symbol"])
    _set_delta_leverage(product_id, config.LEVERAGE)

    path = "/v2/orders"
    url = f"{DELTA_BASE_URL}{path}"
    timestamp = str(int(time.time()))
    payload = {
        "product_id": product_id,
        "size": int(max(1, lots)),
        "side": side.lower(),
        "order_type": "market_order",
        "bracket_stop_loss_price": str(round(stop_loss, int(params.get("decimals", 2)))),
        "bracket_take_profit_price": str(round(take_profit, int(params.get("decimals", 2)))),
        "bracket_stop_loss_limit_price": str(round(stop_loss, int(params.get("decimals", 2)))),
        "bracket_take_profit_limit_price": str(round(take_profit, int(params.get("decimals", 2)))),
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
            log.info("Delta Order Placed [%s pid=%d] (%d lots): %s", symbol, product_id, lots, r.json())
            return r.json()
        else:
            log.error("Delta Order Failed [%s pid=%d]: %s", symbol, product_id, r.text)
    except Exception as e:
        log.error("Delta API Exception [%s]: %s", symbol, e)
    return None

def get_15d_compounding_state(conn) -> dict:
    """
    Calculates 15-Day Cycle Performance, Compounding Active Capital, Lot Multiplier,
    and Free Withdrawable Profit Vault (after all brokerage & GST deductions).
    """
    now_dt = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT realized_pnl_usd, total_fee_usd, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND closed_at_utc IS NOT NULL ORDER BY id ASC"
    ).fetchall()

    total_net_pnl = sum(float(r["realized_pnl_usd"] or 0.0) for r in rows)
    total_brokerage = sum(float(r["total_fee_usd"] or 0.0) for r in rows)
    total_gross_pnl = round(total_net_pnl + total_brokerage, 2)

    cycle_days = int(getattr(config, "CYCLE_DAYS", 15))
    withdraw_pct = float(getattr(config, "WITHDRAWAL_RESERVE_PCT", 0.30))
    reinvest_pct = float(getattr(config, "COMPOUND_REINVEST_PCT", 0.70))

    cutoff_ts = now_dt.timestamp() - (cycle_days * 86400)
    cycle_15d_pnl = 0.0
    cycle_15d_fees = 0.0
    cycle_15d_trades = 0
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r["closed_at_utc"]))
            if dt.timestamp() >= cutoff_ts:
                cycle_15d_pnl += float(r["realized_pnl_usd"] or 0.0)
                cycle_15d_fees += float(r["total_fee_usd"] or 0.0)
                cycle_15d_trades += 1
        except Exception:
            pass

    if total_net_pnl > 0:
        withdrawable_usd = round(total_net_pnl * withdraw_pct, 2)
        reinvested_usd = round(total_net_pnl * reinvest_pct, 2)
        active_trading_equity = round(config.START_EQUITY + reinvested_usd, 2)
    else:
        withdrawable_usd = 0.0
        reinvested_usd = round(total_net_pnl, 2)
        active_trading_equity = round(max(6.0, config.START_EQUITY + total_net_pnl), 2)

    raw_mult = active_trading_equity / max(1.0, float(config.START_EQUITY))
    if cycle_15d_pnl < 0:
        dd_ratio = max(0.70, 1.0 + (cycle_15d_pnl / max(10.0, float(config.START_EQUITY))))
        lot_multiplier = round(min(raw_mult, dd_ratio), 2)
    else:
        lot_multiplier = round(max(1.0, min(3.0, raw_mult)), 2)

    return {
        "base_capital_usd": round(float(config.START_EQUITY), 2),
        "total_gross_pnl_usd": total_gross_pnl,
        "total_brokerage_usd": round(total_brokerage, 2),
        "total_net_pnl_usd": round(total_net_pnl, 2),
        "cycle_days": cycle_days,
        "cycle_15d_net_pnl_usd": round(cycle_15d_pnl, 2),
        "cycle_15d_fees_usd": round(cycle_15d_fees, 2),
        "cycle_15d_trades": cycle_15d_trades,
        "withdrawable_usd": withdrawable_usd,
        "reinvested_usd": reinvested_usd,
        "active_trading_equity_usd": active_trading_equity,
        "total_portfolio_equity_usd": round(config.START_EQUITY + total_net_pnl, 2),
        "lot_multiplier": lot_multiplier,
        "scaling_status": "SCALING UP (PROFIT)" if cycle_15d_pnl > 0 else ("SCALING DOWN (DEFENSIVE)" if cycle_15d_pnl < 0 else "BASE LOT SIZE"),
    }

def get_equity(conn) -> float:
    state = get_15d_compounding_state(conn)
    return state["active_trading_equity_usd"]

def _check_cooldown_and_circuit_breakers(conn, symbol: str = None) -> tuple:
    now_dt = datetime.now(timezone.utc)
    today_start = now_dt.strftime("%Y-%m-%d 00:00:00")

    cnt = conn.execute(
        "SELECT COUNT(*) as c FROM trades WHERE opened_at_utc >= ?", (today_start,)
    ).fetchone()["c"]
    if cnt >= config.MAX_DAILY_TRADES:
        return False, f"Daily limit reached ({cnt}/{config.MAX_DAILY_TRADES} trades)."

    day_pnl = float(conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as s FROM trades WHERE closed_at_utc >= ?", (today_start,)
    ).fetchone()["s"] or 0.0)
    if day_pnl <= -abs(config.MAX_DAILY_LOSS):
        return False, f"Daily Circuit Breaker Triggered: Net loss ${day_pnl:.2f} <= -${config.MAX_DAILY_LOSS:.2f}."

    if symbol:
        recent_closed = conn.execute(
            "SELECT close_reason, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND symbol = ? AND closed_at_utc IS NOT NULL ORDER BY id DESC LIMIT 2",
            (symbol,)
        ).fetchall()
    else:
        recent_closed = conn.execute(
            "SELECT close_reason, closed_at_utc FROM trades WHERE stage = 'CLOSED' AND closed_at_utc IS NOT NULL ORDER BY id DESC LIMIT 2"
        ).fetchall()

    if len(recent_closed) >= 2:
        reasons = [r["close_reason"] or "" for r in recent_closed]
        if all("STOP_LOSS" in r for r in reasons):
            try:
                last_dt = datetime.fromisoformat(recent_closed[0]["closed_at_utc"])
                elapsed_pause = (now_dt - last_dt).total_seconds()
                pause_limit = getattr(config, "CONSECUTIVE_SL_PAUSE_SEC", 9000)
                if elapsed_pause < pause_limit:
                    rem_m = int((pause_limit - elapsed_pause) // 60)
                    return False, f"Circuit Breaker Active: 2 Consecutive SLs hit -> Paused for {rem_m}m to protect account."
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

def calculate_small_cap_delta_size(equity_usd: float, entry: float, sl: float, symbol: str = "BTCUSDT") -> tuple:
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0, 0, 0.0

    params = config.get_asset_params(symbol)
    lot_size = params["lot_size"]
    max_lots = getattr(config, "MAX_LOTS_CAP", 15)
    min_risk = getattr(config, "MIN_RISK_USD", 0.50)
    max_risk = getattr(config, "MAX_RISK_USD", 2.20)

    scale_factor = max(0.65, min(3.0, equity_usd / max(1.0, float(config.START_EQUITY))))
    target_risk = max(min_risk * min(1.0, scale_factor), min(max_risk * scale_factor, equity_usd * float(config.RISK_PCT)))
    raw_qty = target_risk / risk_per_unit

    lots = max(1, min(max_lots, int(round(raw_qty / lot_size))))
    qty = round(lots * lot_size, 4)
    actual_risk_usd = round(qty * risk_per_unit, 2)

    return qty, lots, actual_risk_usd

def open_paper_trade(signal: dict, fencing_token: int):
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token):
            return
        if database.get_setting("auto_trade_enabled", "1") != "1":
            return

        sym = signal.get("symbol", config.SYMBOL)

        act = conn.execute("SELECT id FROM trades WHERE stage != 'CLOSED' AND symbol = ?", (sym,)).fetchone()
        if act:
            return

        active_rows = conn.execute("SELECT symbol, direction FROM trades WHERE stage != 'CLOSED'").fetchall()
        if len(active_rows) >= getattr(config, "MAX_OPEN_TRADES", 3):
            return

        is_gold_sym = ("PAXG" in sym) or ("XAU" in sym) or ("GOLD" in sym)
        if not is_gold_sym:
            same_dir_crypto = sum(
                1 for r in active_rows
                if r["direction"] == signal["direction"] and not any(g in str(r["symbol"]).upper() for g in ("PAXG", "XAU", "GOLD"))
            )
            if same_dir_crypto >= 2:
                log.info("Trade skipped [%s %s]: Max 2 correlated crypto trades in same direction already active.", sym, signal["direction"])
                return

        allowed, reason = _check_cooldown_and_circuit_breakers(conn, sym)
        if not allowed:
            log.info("Trade skipped by Circuit Breaker for %s: %s", sym, reason)
            return

        entry, sl = float(signal["entry"]), float(signal["sl"])
        if abs(entry - sl) <= 0:
            return

        candle_ts = int(signal.get("candle_time") or (int(time.time()) // 900) * 900)
        sig_key = f"{sym}_{signal['direction']}_{candle_ts}"
        existing_sig = conn.execute("SELECT id FROM trades WHERE signal_key = ?", (sig_key,)).fetchone()
        if existing_sig:
            return

        comp = get_15d_compounding_state(conn)
        equity = comp["active_trading_equity_usd"]
        qty, lots, risk_usd = calculate_small_cap_delta_size(equity, entry, sl, sym)
        if qty <= 0 or lots <= 0:
            return

        trade_id = f"TRD-{int(time.time() * 1000)}"
        now_str = database.now_iso()
        inr_rate = getattr(config, "USD_INR_RATE", 86.0)
        risk_inr = round(risk_usd * inr_rate, 0)
        params = config.get_asset_params(sym)
        dec = int(params.get("decimals", 2))
        sl_points = round(abs(entry - sl), dec)

        conn.execute("""
            INSERT INTO trades (trade_id, signal_key, symbol, direction, setup_name, score, entry_price, stop_price, tp1_price, tp2_price, tp3_price, initial_qty, remaining_qty, stage, opened_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
        """, (
            trade_id, sig_key, sym, signal["direction"], signal["setup_name"],
            signal["score"], entry, sl, signal["tp1"], signal["tp2"], signal["tp3"],
            qty, qty, now_str, now_str
        ))
        conn.commit()

        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(
                f"🚀 *HIGH-CONVICTION TRADE OPENED ({params['display_name']} {signal['direction']})*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Setup: {signal['setup_name']}\n"
                f"• Entry: ${entry:,.{dec}f} | SL: ${sl:,.{dec}f} ({sl_points} pts)\n"
                f"• TP1: ${signal['tp1']:,.{dec}f} | TP2: ${signal['tp2']:,.{dec}f} | Runner: ${signal['tp3']:,.{dec}f}\n"
                f"• Size: {lots} Lot ({qty}) [{comp['lot_multiplier']:.2f}x Scale]\n"
                f"• Max Risk: ${risk_usd:.2f} (~₹{risk_inr:.0f})\n"
                f"• Active Cap: ${equity:.2f} | Free Vault: ${comp['withdrawable_usd']:.2f}"
            )

        if config.MODE in ("TESTNET", "LIVE"):
            delta_side = "buy" if signal["direction"] == "LONG" else "sell"
            place_delta_order(sym, delta_side, lots, sl, signal["tp2"])
    finally:
        conn.close()

def _manage_single_position(conn, trade_dict: dict, current_price: float, fencing_token: int):
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
    sym = trade_dict.get("symbol") or "BTCUSDT"
    now_str = database.now_iso()
    is_long = direction == "LONG"
    params = config.get_asset_params(sym)
    dec = int(params.get("decimals", 2))

    sl_hit = (is_long and current_price <= sl) or (not is_long and current_price >= sl)
    if sl_hit:
        exec_exit = sl if config.MODE == "PAPER" else current_price
        price_diff = (exec_exit - entry) if is_long else (entry - exec_exit)
        fee_usd = round((entry + exec_exit) * rem_qty * float(config.FEE_RATE), 3)
        leg_pnl = round((price_diff * rem_qty) - fee_usd, 2)
        total_realized = round(float(trade_dict["realized_pnl_usd"]) + leg_pnl, 2)
        in_profit_sl = (is_long and sl >= entry) or (not is_long and sl <= entry)
        close_reason = "BREAK_EVEN_EXIT" if (tp1_done or in_profit_sl) else "STOP_LOSS_HIT"

        conn.execute("""
            UPDATE trades
            SET stage = 'CLOSED', remaining_qty = 0, realized_pnl_usd = ?, unrealized_pnl_usd = 0,
                total_fee_usd = total_fee_usd + ?, close_reason = ?, closed_at_utc = ?, updated_at_utc = ?
            WHERE trade_id = ?
        """, (total_realized, fee_usd, close_reason, now_str, now_str, tid))
        conn.commit()

        msg_title = "🛡️ BREAK-EVEN SL HIT (PROFIT SECURED)" if (tp1_done or in_profit_sl) else "🛑 STOP LOSS HIT"
        total_fees_trade = round(float(trade_dict.get("total_fee_usd") or 0.0) + fee_usd, 3)
        gross_trade = round(total_realized + total_fees_trade, 2)
        comp = get_15d_compounding_state(conn)
        if hasattr(notifications, "enqueue_telegram"):
            notifications.enqueue_telegram(
                f"{msg_title} ({params['display_name']} {direction})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Exit Price: ${exec_exit:,.{dec}f}\n"
                f"• Gross PnL: ${gross_trade:+.2f} | Brokerage: -${total_fees_trade:.2f}\n"
                f"• Net Profit Added: ${total_realized:+.2f} USD (₹{total_realized*config.USD_INR_RATE:+.0f})\n"
                f"• 15D Withdrawable Vault: ${comp['withdrawable_usd']:.2f} (₹{comp['withdrawable_usd']*config.USD_INR_RATE:.0f})\n"
                f"• Reason: {close_reason}"
            )
        return

    if not tp1_done and trade_dict.get("opened_at_utc"):
        try:
            open_dt = datetime.fromisoformat(trade_dict["opened_at_utc"])
            now_dt = datetime.fromisoformat(now_str)
            elapsed_sec = (now_dt - open_dt).total_seconds()
            adverse_dist = (entry - current_price) if is_long else (current_price - entry)
            trigger_dist = (params["default_atr"] * 0.60)
            if elapsed_sec >= 14400 and adverse_dist >= trigger_dist:
                exec_exit = current_price
                price_diff = (exec_exit - entry) if is_long else (entry - exec_exit)
                fee_usd = round((entry + exec_exit) * rem_qty * float(config.FEE_RATE), 3)
                leg_pnl = round((price_diff * rem_qty) - fee_usd, 2)
                total_realized = round(float(trade_dict["realized_pnl_usd"]) + leg_pnl, 2)
                close_reason = "STRUCTURAL_INVALIDATION_EXIT"

                conn.execute("""
                    UPDATE trades
                    SET stage = 'CLOSED', remaining_qty = 0, realized_pnl_usd = ?, unrealized_pnl_usd = 0,
                        total_fee_usd = total_fee_usd + ?, close_reason = ?, closed_at_utc = ?, updated_at_utc = ?
                    WHERE trade_id = ?
                """, (total_realized, fee_usd, close_reason, now_str, now_str, tid))
                conn.commit()

                if hasattr(notifications, "enqueue_telegram"):
                    notifications.enqueue_telegram(
                        f"⚠️ *EARLY STRUCTURAL EXIT ({params['display_name']} {direction})*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• Exit Price: ${exec_exit:,.{dec}f}\n"
                        f"• Net PnL (After Fee): ${total_realized:+.2f} USD\n"
                        f"• Reason: Momentum stalled for >4 hours; exited early to avoid full SL."
                    )
                return
        except Exception:
            pass

    if getattr(config, "ENABLE_DYNAMIC_PROFIT_LOCK", True) and not tp1_done:
        sl_dist = abs(entry - sl)
        if sl_dist > 0:
            current_gain = (current_price - entry) if is_long else (entry - current_price)
            r_mult = current_gain / sl_dist
            if r_mult >= 1.0:
                fee_distance = entry * float(config.FEE_RATE) * 2.4
                be_buffer = max(float(params.get("be_buffer", 0.25)), fee_distance + (sl_dist * 0.12))
                target_sl = round(entry + be_buffer if is_long else entry - be_buffer, dec)
                if (is_long and target_sl > sl) or (not is_long and target_sl < sl):
                    conn.execute("UPDATE trades SET stop_price = ?, updated_at_utc = ? WHERE trade_id = ?", (target_sl, now_str, tid))
                    conn.commit()
                    if hasattr(notifications, "enqueue_telegram"):
                        notifications.enqueue_telegram(
                            f"🛡️ *PROFIT LOCK (+1.0R RUN)*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"• {params['display_name']} is up +1.0R (+${current_gain:.{dec}f})!\n"
                            f"• SL Trailed to Fee-Protected Profit: ${target_sl:,.{dec}f}"
                        )
            elif r_mult >= 0.80:
                tight_dist = sl_dist * 0.25
                target_sl = round(entry - tight_dist if is_long else entry + tight_dist, dec)
                if (is_long and target_sl > sl) or (not is_long and target_sl < sl):
                    conn.execute("UPDATE trades SET stop_price = ?, updated_at_utc = ? WHERE trade_id = ?", (target_sl, now_str, tid))
                    conn.commit()

    if not tp1_done:
        if (is_long and current_price >= tp1) or (not is_long and current_price <= tp1):
            booked_qty = round(init_qty * 0.50, 6)
            new_rem_qty = max(0.0, round(rem_qty - booked_qty, 6))
            gain_pts = (tp1 - entry) if is_long else (entry - tp1)
            fee_usd = round((entry + tp1) * booked_qty * float(config.FEE_RATE), 3)
            booked_pnl = round((gain_pts * booked_qty) - fee_usd, 2)
            fee_distance = entry * float(config.FEE_RATE) * 2.4
            be_offset = max(float(params.get("be_buffer", 0.3)) * 1.25, fee_distance + (abs(entry - sl) * 0.18))
            new_sl = round(entry + be_offset if is_long else entry - be_offset, dec)

            conn.execute("""
                UPDATE trades
                SET tp1_done = 1, remaining_qty = ?, stop_price = ?,
                    realized_pnl_usd = realized_pnl_usd + ?, total_fee_usd = total_fee_usd + ?, updated_at_utc = ?
                WHERE trade_id = ?
            """, (new_rem_qty, new_sl, booked_pnl, fee_usd, now_str, tid))
            conn.commit()

            if hasattr(notifications, "enqueue_telegram"):
                notifications.enqueue_telegram(
                    f"🎯 *TARGET 1 HIT ({params['display_name']})*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• TP1: ${tp1:,.{dec}f}\n"
                    f"• Net Locked Profit (After Fee): ${booked_pnl:+.2f} USD\n"
                    f"• SL Trailed to Fee-Protected BE: ${new_sl:,.{dec}f}"
                )
            return

    if tp1_done and not tp2_done:
        if (is_long and current_price >= tp2) or (not is_long and current_price <= tp2):
            booked_qty = round(init_qty * 0.30, 6)
            new_rem_qty = max(0.0, round(rem_qty - booked_qty, 6))
            gain_pts = (tp2 - entry) if is_long else (entry - tp2)
            fee_usd = round((entry + tp2) * booked_qty * float(config.FEE_RATE), 3)
            booked_pnl = round((gain_pts * booked_qty) - fee_usd, 2)
            new_sl = round(tp1, dec)

            conn.execute("""
                UPDATE trades
                SET tp2_done = 1, remaining_qty = ?, stop_price = ?,
                    realized_pnl_usd = realized_pnl_usd + ?, total_fee_usd = total_fee_usd + ?, updated_at_utc = ?
                WHERE trade_id = ?
            """, (new_rem_qty, new_sl, booked_pnl, fee_usd, now_str, tid))
            conn.commit()

            if hasattr(notifications, "enqueue_telegram"):
                notifications.enqueue_telegram(
                    f"🎯 *TARGET 2 HIT ({params['display_name']})*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• TP2: ${tp2:,.{dec}f}\n"
                    f"• Net Locked Profit (After Fee): ${booked_pnl:+.2f} USD\n"
                    f"• SL Trailed to TP1: ${new_sl:,.{dec}f}\n"
                    f"• 20% Runner Trailing for Target 3 (${tp3:,.{dec}f})!"
                )
            return

    if tp1_done and tp2_done:
        if (is_long and current_price >= tp3) or (not is_long and current_price <= tp3):
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

            total_fees_trade = round(float(trade_dict.get("total_fee_usd") or 0.0) + fee_usd, 3)
            gross_trade = round(total_realized + total_fees_trade, 2)
            comp = get_15d_compounding_state(conn)
            if hasattr(notifications, "enqueue_telegram"):
                notifications.enqueue_telegram(
                    f"🏆 *TARGET 3 FULL EXIT ({params['display_name']})*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• Final Target: ${tp3:,.{dec}f}\n"
                    f"• Gross PnL: ${gross_trade:+.2f} | Brokerage: -${total_fees_trade:.2f}\n"
                    f"• Net Profit Added: ${total_realized:+.2f} USD (₹{total_realized*config.USD_INR_RATE:+.0f})\n"
                    f"• 15D Withdrawable Vault: ${comp['withdrawable_usd']:.2f} (₹{comp['withdrawable_usd']*config.USD_INR_RATE:.0f})"
                )
            return

    cur_pnl = round(((current_price - entry) if is_long else (entry - current_price)) * rem_qty, 2)
    conn.execute("UPDATE trades SET unrealized_pnl_usd = ?, updated_at_utc = ? WHERE trade_id = ?", (cur_pnl, now_str, tid))
    conn.commit()

def manage_all_positions(fencing_token: int, lock):
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token):
            return
        active_trades = conn.execute("SELECT * FROM trades WHERE stage != 'CLOSED'").fetchall()
        if not active_trades:
            return
        for row in active_trades:
            trade_dict = dict(row)
            sym = trade_dict.get("symbol") or "BTCUSDT"
            with lock:
                cur_p = market_data.get_market(sym).get("price")
            if cur_p and cur_p > 0:
                _manage_single_position(conn, trade_dict, cur_p, fencing_token)
    except Exception as e:
        log.error("Error managing positions: %s", e, exc_info=True)
    finally:
        conn.close()

def manage_positions(current_price: float, fencing_token: int, klines_15m: list = None, current_atr: float = None):
    conn = database.db()
    try:
        if not database.leader_ok(fencing_token):
            return
        active = conn.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 1").fetchone()
        if active:
            _manage_single_position(conn, dict(active), current_price, fencing_token)
    finally:
        conn.close()
