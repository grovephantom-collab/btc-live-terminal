import logging
import config, market_data

log = logging.getLogger("SMCEngine")

def _parse_candle(c):
    if isinstance(c, dict):
        return float(c.get("open", 0.0)), float(c.get("high", 0.0)), float(c.get("low", 0.0)), float(c.get("close", 0.0)), float(c.get("vol", 1.0)), int(c.get("time", 0))
    return float(c[1]), float(c[2]), float(c[3]), float(c[4]), (float(c[5]) if len(c) > 5 else 1.0), (int(c[0]) if len(c) > 0 else 0)

def _calc_ema(values: list, period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v * k) + (ema * (1.0 - k))
    return ema

def _calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return 100.0 if avg_loss <= 1e-9 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

def _calc_atr(parsed_candles: list, period: int = 14, default_atr: float = 220.0) -> float:
    if len(parsed_candles) < 2:
        return default_atr
    trs = [max(parsed_candles[i][1] - parsed_candles[i][2], abs(parsed_candles[i][1] - parsed_candles[i - 1][3]), abs(parsed_candles[i][2] - parsed_candles[i - 1][3])) for i in range(1, len(parsed_candles))]
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)

def _calc_adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    n = len(closes)
    if n < period * 2:
        return 25.0
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        hd, ld = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        plus_dm.append(hd if (hd > ld and hd > 0) else 0.0)
        minus_dm.append(ld if (ld > hd and ld > 0) else 0.0)
    tr_s, p_s, m_s, dx = sum(tr[:period]), sum(plus_dm[:period]), sum(minus_dm[:period]), []
    for i in range(period, len(tr)):
        tr_s = tr_s - (tr_s / period) + tr[i]
        p_s = p_s - (p_s / period) + plus_dm[i]
        m_s = m_s - (m_s / period) + minus_dm[i]
        dip, dim = (p_s / (tr_s + 1e-9)) * 100.0, (m_s / (tr_s + 1e-9)) * 100.0
        dx.append((abs(dip - dim) / (dip + dim + 1e-9)) * 100.0)
    if len(dx) < period:
        return sum(dx) / max(1, len(dx))
    adx = sum(dx[:period]) / period
    for d in dx[period:]:
        adx = (adx * (period - 1) + d) / period
    return round(float(adx), 1)

def _calc_vwap(closed_15: list, lookback: int = 48) -> float:
    bars = closed_15[-lookback:] if len(closed_15) >= lookback else closed_15
    if not bars:
        return 0.0
    pv = sum(((b[1] + b[2] + b[3]) / 3.0) * max(1e-6, b[4]) for b in bars)
    vv = sum(max(1e-6, b[4]) for b in bars)
    return pv / max(1e-6, vv)

def _detect_order_blocks(closed_15: list, atr: float) -> tuple:
    if len(closed_15) < 12:
        return None, None
    bull_ob, bear_ob = None, None
    recent = closed_15[-18:-1]
    for i in range(len(recent) - 2):
        c0, c1, c2 = recent[i], recent[i + 1], recent[i + 2]
        if c0[3] < c0[0] and c1[3] > c1[0] and (c2[3] - c0[2]) >= (atr * 0.80) and c1[3] > c0[1]:
            bull_ob = (c0[2], c0[1])
        if c0[3] > c0[0] and c1[3] < c1[0] and (c0[1] - c2[3]) >= (atr * 0.80) and c1[3] < c0[2]:
            bear_ob = (c0[2], c0[1])
    return bull_ob, bear_ob

def _detect_fvg_and_displacement(closed_15: list, atr: float, min_gap: float) -> tuple:
    if len(closed_15) < 6:
        return False, False
    bull_fvg, bear_fvg = False, False
    gap_thresh = max(min_gap * 0.30, atr * 0.10)
    for i in range(len(closed_15) - 4, len(closed_15)):
        c_m2, c_m1, c_cur = closed_15[i - 2], closed_15[i - 1], closed_15[i]
        if (c_cur[2] - c_m2[1]) >= gap_thresh and (c_m1[3] > c_m1[0]):
            bull_fvg = True
        if (c_m2[2] - c_cur[1]) >= gap_thresh and (c_m1[3] < c_m1[0]):
            bear_fvg = True
    lb = closed_15[-1]
    if lb[3] > lb[0] and abs(lb[3] - lb[0]) >= (atr * 0.55):
        bull_fvg = True
    if lb[3] < lb[0] and abs(lb[3] - lb[0]) >= (atr * 0.55):
        bear_fvg = True
    return bull_fvg, bear_fvg

def get_structure(market_dict: dict, symbol: str = "BTCUSDT") -> dict:
    klines = market_dict.get("klines", {})
    k15, k1h, k4h = klines.get("15m", []), klines.get("1h", []), klines.get("4h", [])
    if len(k15) < 35:
        return {}
    p15 = [_parse_candle(c) for c in k15]
    closed_15 = p15[:-1]
    highs, lows, closes, vols = [p[1] for p in closed_15], [p[2] for p in closed_15], [p[3] for p in closed_15], [p[4] for p in closed_15]
    cur_close = closes[-1]
    asset_params = config.get_asset_params(symbol, market_dict.get("price") or cur_close)
    atr_15m = _calc_atr(closed_15, 14, default_atr=asset_params["default_atr"])
    rsi_15m = _calc_rsi(closes, 14)
    adx_15m = _calc_adx(highs, lows, closes, 14)
    vol_sma20 = sum(vols[-20:]) / min(len(vols), 20)
    ema9, ema20, ema50, ema200 = _calc_ema(closes, 9), _calc_ema(closes, 20), _calc_ema(closes, 50), _calc_ema(closes, min(len(closes), 200))
    bb_mid = sum(closes[-20:]) / 20.0
    bb_std = (sum((x - bb_mid) ** 2 for x in closes[-20:]) / 20.0) ** 0.5
    bb_bandwidth = (4.0 * bb_std) / (bb_mid + 1e-9)

    swing_lookback = min(len(closed_15) - 4, getattr(config, "SWING_LOOKBACK_BARS", 96))
    swing_high, swing_low = max(highs[-swing_lookback - 3 : -3]), min(lows[-swing_lookback - 3 : -3])
    pdh, pdl, daily_open = market_dict.get("pdh"), market_dict.get("pdl"), market_dict.get("daily_open")
    day_high_24h, day_low_24h = market_dict.get("day_high_24h"), market_dict.get("day_low_24h")
    asian_high, asian_low = market_dict.get("asian_high"), market_dict.get("asian_low")

    choch_status, choch_level = "NONE", None
    if len(highs) >= 20:
        s_floor, s_ceil = min(lows[-17:-2]), max(highs[-17:-2])
        if cur_close < s_floor and closes[-1] < closes[-2]:
            choch_status, choch_level = "BEARISH_CHOCH", s_floor
        elif cur_close > s_ceil and closes[-1] > closes[-2]:
            choch_status, choch_level = "BULLISH_CHOCH", s_ceil

    bull_score = 1 if cur_close > ema200 else 0
    bear_score = 0 if cur_close > ema200 else 1
    htf_1h_bias = "NEUTRAL"
    if len(k1h) >= 15:
        p1h = [_parse_candle(c) for c in k1h[:-1]]
        c1h = [p[3] for p in p1h]
        e20_1h, e50_1h, e200_1h = _calc_ema(c1h, 20), _calc_ema(c1h, 50), _calc_ema(c1h, min(len(c1h), 200))
        if c1h[-1] > e200_1h:
            bull_score += 2
        else:
            bear_score += 2
        if c1h[-1] >= e20_1h and e50_1h >= e200_1h * 0.998:
            bull_score += 1
            htf_1h_bias = "BULLISH"
        elif c1h[-1] <= e20_1h and e50_1h <= e200_1h * 1.002:
            bear_score += 1
            htf_1h_bias = "BEARISH"

    htf_4h_bias = "NEUTRAL"
    if len(k4h) >= 10:
        c4h = [_parse_candle(c)[3] for c in k4h[:-1]]
        e50_4h, e200_4h = _calc_ema(c4h, min(len(c4h), 50)), _calc_ema(c4h, min(len(c4h), 200))
        if c4h[-1] > e50_4h and e50_4h >= e200_4h * 0.995:
            bull_score += 2
            htf_4h_bias = "BULLISH"
        elif c4h[-1] < e50_4h and e50_4h <= e200_4h * 1.005:
            bear_score += 2
            htf_4h_bias = "BEARISH"

    if daily_open and daily_open > 0:
        if cur_close > daily_open:
            bull_score += 2
        else:
            bear_score += 2

    if (bull_score >= 5 and bull_score > bear_score) or (bull_score >= 4 and bear_score <= 2):
        macro_bias = "BULLISH"
    elif (bear_score >= 5 and bear_score > bull_score) or (bear_score >= 4 and bull_score <= 2):
        macro_bias = "BEARISH"
    else:
        macro_bias = "NEUTRAL"

    bull_fvg, bear_fvg = _detect_fvg_and_displacement(closed_15, atr_15m, asset_params["min_gap"])
    vwap_15m = _calc_vwap(closed_15, 48)
    bull_ob, bear_ob = _detect_order_blocks(closed_15, atr_15m)
    is_choppy = (adx_15m < 18.0) and ((bb_bandwidth < 0.0035) or (abs(ema20 - ema50) / max(1e-9, ema50) < 0.0012))

    return {
        "symbol": symbol, "swing_high": swing_high, "swing_low": swing_low, "pdh": pdh, "pdl": pdl,
        "daily_open": daily_open, "day_high_24h": day_high_24h, "day_low_24h": day_low_24h,
        "asian_high": asian_high, "asian_low": asian_low, "current_close": cur_close,
        "ema9": ema9, "ema20": ema20, "ema50": ema50, "ema200": ema200, "rsi": rsi_15m,
        "adx": adx_15m, "bb_bandwidth": bb_bandwidth, "vol_sma20": vol_sma20, "atr": atr_15m,
        "choch_status": choch_status, "choch_level": choch_level, "macro_bias": macro_bias,
        "htf_bias": macro_bias, "htf_1h_bias": htf_1h_bias, "htf_4h_bias": htf_4h_bias,
        "bull_fvg": bull_fvg, "bear_fvg": bear_fvg, "vwap": vwap_15m, "bull_ob": bull_ob,
        "bear_ob": bear_ob, "is_choppy": is_choppy, "funding_rate": market_dict.get("funding_rate", 0.0001),
        "in_killzone": market_dict.get("in_killzone", False), "killzone_name": market_dict.get("killzone_name", "OFF_HOURS"),
        "is_dead_hour": market_dict.get("is_dead_hour", False), "news_blackout": market_dict.get("news_blackout", False),
        "cvd_ratio": market_dict.get("cvd_ratio", 0.50),
    }

def _btc_leader_allows(symbol: str, direction: str) -> bool:
    if symbol.strip().upper() in ("BTCUSDT", "PAXGUSDT", "XAUUSD"):
        return True
    try:
        btc_m = market_data.get_market("BTCUSDT")
        if not btc_m or not btc_m.get("klines"):
            return True
        btc_cvd, btc_p, btc_open = float(btc_m.get("cvd_ratio") or 0.50), float(btc_m.get("price") or 0.0), float(btc_m.get("daily_open") or 0.0)
        if direction == "LONG" and btc_cvd < 0.44 and btc_open > 0 and btc_p < btc_open:
            return False
        if direction == "SHORT" and btc_cvd > 0.56 and btc_open > 0 and btc_p > btc_open:
            return False
    except Exception:
        pass
    return True

def _build_trade_levels(direction: str, entry: float, raw_sl: float, atr: float, setup_name: str, score: float, candle_time: int, symbol: str = "BTCUSDT") -> dict:
    params = config.get_asset_params(symbol, entry)
    dec = int(params.get("decimals", 2))
    wick_pad = max(params["sl_buffer"], atr * 0.35)
    if direction == "LONG":
        sl_dist = max(params["min_stop"], min(params["max_stop"], max((entry - raw_sl) + wick_pad, atr * 1.65)))
        sl, tp1, tp2, tp3 = round(entry - sl_dist, dec), round(entry + sl_dist * 1.00, dec), round(entry + sl_dist * 1.85, dec), round(entry + sl_dist * 2.90, dec)
    else:
        sl_dist = max(params["min_stop"], min(params["max_stop"], max((raw_sl - entry) + wick_pad, atr * 1.65)))
        sl, tp1, tp2, tp3 = round(entry + sl_dist, dec), round(entry - sl_dist * 1.00, dec), round(entry - sl_dist * 1.85, dec), round(entry - sl_dist * 2.90, dec)
    return {"symbol": symbol, "direction": direction, "setup_name": setup_name, "score": round(score, 1), "entry": round(entry, dec), "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "candle_time": candle_time}

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float, symbol: str = "BTCUSDT") -> dict:
    if len(candles_15m) < 35 or not structure or structure.get("news_blackout", False):
        return None
    if getattr(config, "FILTER_DEAD_HOURS", True) and structure.get("is_dead_hour", False):
        return None
    if getattr(config, "STRICT_KILLZONE_ONLY", False) and not structure.get("in_killzone", False):
        return None

    params = config.get_asset_params(symbol, current_price or structure.get("current_close"))
    funding = structure.get("funding_rate", 0.0001)
    max_long_f, min_short_f = getattr(config, "MAX_FUNDING_FOR_LONG", 0.00035), getattr(config, "MIN_FUNDING_FOR_SHORT", -0.00020)
    is_choppy, adx = structure.get("is_choppy", False), structure.get("adx", 20.0)
    macro_bias = structure.get("macro_bias", "NEUTRAL")
    htf_1h_bias = structure.get("htf_1h_bias", "NEUTRAL")
    htf_4h_bias = structure.get("htf_4h_bias", "NEUTRAL")
    bull_fvg, bear_fvg = structure.get("bull_fvg", False), structure.get("bear_fvg", False)
    vwap = float(structure.get("vwap") or 0.0)
    bull_ob, bear_ob = structure.get("bull_ob"), structure.get("bear_ob")
    cvd_ratio = structure.get("cvd_ratio", 0.50)

    parsed = [_parse_candle(c) for c in candles_15m]
    c, prev = parsed[-2], parsed[-3]
    o, h, l, cl, vol, c_time = c[0], c[1], c[2], c[3], c[4], c[5]
    prev_o, prev_h, prev_l, prev_cl = prev[0], prev[1], prev[2], prev[3]
    atr = structure.get("atr", params["default_atr"])
    sh, sl = structure["swing_high"], structure["swing_low"]
    pdh, pdl = structure.get("pdh"), structure.get("pdl")
    day_high_24h, day_low_24h = structure.get("day_high_24h"), structure.get("day_low_24h")
    ema9, ema20, ema50, ema200 = structure["ema9"], structure["ema20"], structure["ema50"], structure["ema200"]
    rsi, vol_sma20 = structure["rsi"], structure.get("vol_sma20", 1.0)
    choch_status, choch_level = structure.get("choch_status", "NONE"), structure.get("choch_level")

    if current_price and abs(current_price - cl) > (atr * 1.45):
        return None

    candle_range = max(1e-6, h - l)
    body_ratio = abs(cl - o) / candle_range
    lower_wick_ratio, upper_wick_ratio = (min(o, cl) - l) / candle_range, (h - max(o, cl)) / candle_range
    recent_closed = parsed[-7:-1]
    lowest_recent, highest_recent = min(x[2] for x in recent_closed), max(x[1] for x in recent_closed)

    range_ceil = max([x for x in [pdh, day_high_24h, sh] if x and x > 0] or [cl + atr * 3.0])
    range_floor = min([x for x in [pdl, day_low_24h, sl] if x and x > 0] or [cl - atr * 3.0])
    rel_pos = (cl - range_floor) / max(atr * 2.0, range_ceil - range_floor)
    in_discount, in_premium = rel_pos < 0.50, rel_pos > 0.50
    allow_long_cvd = (cvd_ratio >= 0.52) if getattr(config, "ENABLE_CVD_FILTER", True) else True
    allow_short_cvd = (cvd_ratio <= 0.48) if getattr(config, "ENABLE_CVD_FILTER", True) else True

    three_bar_run = abs(cl - parsed[-4][0]) if len(parsed) >= 5 else 0.0
    long_htf_ok = (htf_4h_bias != "BEARISH") and (htf_1h_bias != "BEARISH") and _btc_leader_allows(symbol, "LONG") and (40.0 <= rsi <= 68.0) and not (cl > parsed[-4][0] and three_bar_run > atr * 2.1)
    short_htf_ok = (htf_4h_bias != "BULLISH") and (htf_1h_bias != "BULLISH") and _btc_leader_allows(symbol, "SHORT") and (32.0 <= rsi <= 60.0) and not (cl < parsed[-4][0] and three_bar_run > atr * 2.1)

    long_room_ok = (range_ceil - cl) >= (atr * 1.35)
    short_room_ok = (cl - range_floor) >= (atr * 1.35)

    bull_confirm = (cl > o) and (cl >= ema9) and (cl > prev_cl) and (upper_wick_ratio <= 0.30)
    bear_confirm = (cl < o) and (cl <= ema9) and (cl < prev_cl) and (lower_wick_ratio <= 0.30)

    vol_spike_ok = vol >= vol_sma20 * 1.15
    vwap_long_ok = (cl >= vwap * 0.9985) if vwap > 0 else True
    vwap_short_ok = (cl <= vwap * 1.0015) if vwap > 0 else True
    touched_bull_ob = bool(bull_ob and (l <= bull_ob[1] + atr * 0.15) and (cl >= bull_ob[0]))
    touched_bear_ob = bool(bear_ob and (h >= bear_ob[0] - atr * 0.15) and (cl <= bear_ob[1]))
    min_adx = getattr(config, "MIN_ADX_TREND", 19.0)

    # STRATEGY 1: PDH/PDL & ASIAN LIQUIDITY SWEEP REVERSAL
    d_low = pdl or day_low_24h or structure.get("asian_low")
    if d_low and (macro_bias != "BEARISH") and long_htf_ok and long_room_ok and funding <= max_long_f and allow_long_cvd:
        if (lowest_recent <= d_low + atr * 0.08) and (cl > d_low) and bull_confirm and (lower_wick_ratio >= 0.30 or body_ratio >= 0.45):
            return _build_trade_levels("LONG", current_price or cl, lowest_recent, atr, "Sniper PDH/PDL Liquidity Sweep", 98.0, c_time, symbol)
    d_high = pdh or day_high_24h or structure.get("asian_high")
    if d_high and (macro_bias != "BULLISH") and short_htf_ok and short_room_ok and funding >= min_short_f and allow_short_cvd:
        if (highest_recent >= d_high - atr * 0.08) and (cl < d_high) and bear_confirm and (upper_wick_ratio >= 0.30 or body_ratio >= 0.45):
            return _build_trade_levels("SHORT", current_price or cl, highest_recent, atr, "Sniper PDH/PDL Liquidity Sweep", 98.0, c_time, symbol)

    # STRATEGY 2: CHoCH + FVG + VOLUME DISPLACEMENT BREAK
    if getattr(config, "ENABLE_CHOCH_STRATEGY", True) and (not is_choppy) and (adx >= min_adx) and choch_status != "NONE" and choch_level:
        if choch_status == "BEARISH_CHOCH" and macro_bias != "BULLISH" and short_htf_ok and short_room_ok and (not in_discount) and allow_short_cvd and funding >= min_short_f:
            if bear_confirm and (cl < choch_level) and (vol_spike_ok or bear_fvg):
                return _build_trade_levels("SHORT", current_price or cl, max(highest_recent, choch_level + atr * 0.20), atr, "Sniper CHoCH + FVG Breakdown", 99.0, c_time, symbol)
        if choch_status == "BULLISH_CHOCH" and macro_bias != "BEARISH" and long_htf_ok and long_room_ok and (not in_premium) and allow_long_cvd and funding <= max_long_f:
            if bull_confirm and (cl > choch_level) and (vol_spike_ok or bull_fvg):
                return _build_trade_levels("LONG", current_price or cl, min(lowest_recent, choch_level - atr * 0.20), atr, "Sniper CHoCH + FVG Breakout", 99.0, c_time, symbol)

    # STRATEGY 3: DAILY EXPANSION BREAKOUT
    if getattr(config, "ENABLE_DAILY_BREAKOUT", True) and (not is_choppy) and (adx >= 20.0):
        bh = pdh or day_high_24h
        if bh and macro_bias == "BULLISH" and long_htf_ok and vwap_long_ok and funding <= max_long_f and allow_long_cvd:
            if (cl > bh) and ((cl - bh) <= atr * 0.38) and (prev_cl <= bh + atr * 0.12) and bull_confirm and (body_ratio >= 0.50) and (vol_spike_ok or bull_fvg):
                return _build_trade_levels("LONG", current_price or cl, min(l, prev_l, ema20), atr, "Sniper Daily High Expansion", 97.5, c_time, symbol)
        bl = pdl or day_low_24h
        if bl and macro_bias == "BEARISH" and short_htf_ok and vwap_short_ok and funding >= min_short_f and allow_short_cvd:
            if (cl < bl) and ((bl - cl) <= atr * 0.38) and (prev_cl >= bl - atr * 0.12) and bear_confirm and (body_ratio >= 0.50) and (vol_spike_ok or bear_fvg):
                return _build_trade_levels("SHORT", current_price or cl, max(h, prev_h, ema20), atr, "Sniper Daily Low Breakdown", 97.5, c_time, symbol)

    # STRATEGY 4: 24H MACRO SWING LIQUIDITY TRAP
    if (macro_bias != "BEARISH") and long_htf_ok and long_room_ok and funding <= max_long_f and in_discount and allow_long_cvd:
        if (lowest_recent <= sl - atr * 0.05) and (cl > sl) and bull_confirm and (lower_wick_ratio >= 0.35 or body_ratio >= 0.48) and (vol_spike_ok or bull_fvg):
            return _build_trade_levels("LONG", current_price or cl, lowest_recent, atr, "24H Macro Swing Liquidity Trap", 96.0, c_time, symbol)
    if (macro_bias != "BULLISH") and short_htf_ok and short_room_ok and funding >= min_short_f and in_premium and allow_short_cvd:
        if (highest_recent >= sh + atr * 0.05) and (cl < sh) and bear_confirm and (upper_wick_ratio >= 0.35 or body_ratio >= 0.48) and (vol_spike_ok or bear_fvg):
            return _build_trade_levels("SHORT", current_price or cl, highest_recent, atr, "24H Macro Swing Liquidity Trap", 96.0, c_time, symbol)

    # STRATEGY 5: EMA20 + ORDER BLOCK + VWAP TREND PULLBACK
    if not is_choppy and adx >= 20.0:
        ec = current_price or cl
        if macro_bias == "BULLISH" and long_htf_ok and long_room_ok and vwap_long_ok and (cl > ema200) and (ema9 > ema20 > ema50) and (((l <= ema20 * 1.002) and (cl >= ema20)) or touched_bull_ob):
            if bull_confirm and (body_ratio >= 0.48) and (bull_fvg or vol >= vol_sma20 * 1.10) and funding <= max_long_f and allow_long_cvd and (not in_premium):
                return _build_trade_levels("LONG", ec, min(l, prev_l, lowest_recent), atr, "Sniper EMA + OB Trend Pullback", 95.0, c_time, symbol)
        if macro_bias == "BEARISH" and short_htf_ok and short_room_ok and vwap_short_ok and (cl < ema200) and (ema9 < ema20 < ema50) and (((h >= ema20 * 0.998) and (cl <= ema20)) or touched_bear_ob):
            if bear_confirm and (body_ratio >= 0.48) and (bear_fvg or vol >= vol_sma20 * 1.10) and funding >= min_short_f and allow_short_cvd and (not in_discount):
                return _build_trade_levels("SHORT", ec, max(h, prev_h, highest_recent), atr, "Sniper EMA + OB Trend Pullback", 95.0, c_time, symbol)

    # STRATEGY 6: STRONG MOMENTUM VWAP + CVD TREND CONTINUATION
    if not is_choppy and adx >= 24.0:
        ec = current_price or cl
        if macro_bias == "BULLISH" and long_htf_ok and long_room_ok and vwap_long_ok and (ema9 > ema20 > ema50) and bull_confirm and (body_ratio >= 0.55) and (bull_fvg or vol_spike_ok) and (cvd_ratio >= 0.58) and funding <= max_long_f and (not in_premium):
            return _build_trade_levels("LONG", ec, min(l, prev_l, ema20), atr, "Momentum VWAP + CVD Continuation", 94.0, c_time, symbol)
        if macro_bias == "BEARISH" and short_htf_ok and short_room_ok and vwap_short_ok and (ema9 < ema20 < ema50) and bear_confirm and (body_ratio >= 0.55) and (bear_fvg or vol_spike_ok) and (cvd_ratio <= 0.42) and funding >= min_short_f and (not in_discount):
            return _build_trade_levels("SHORT", ec, max(h, prev_h, ema20), atr, "Momentum VWAP + CVD Continuation", 94.0, c_time, symbol)

    return None
