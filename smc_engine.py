import logging
import config, market_data

log = logging.getLogger("SMCEngine")

def _parse_candle(c):
    if isinstance(c, dict):
        return (
            float(c.get("open", 0.0)),
            float(c.get("high", 0.0)),
            float(c.get("low", 0.0)),
            float(c.get("close", 0.0)),
            float(c.get("vol", 1.0)),
            int(c.get("time", 0))
        )
    return (
        float(c[1]),
        float(c[2]),
        float(c[3]),
        float(c[4]),
        float(c[5]) if len(c) > 5 else 1.0,
        int(c[0]) if len(c) > 0 else 0
    )

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
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss <= 1e-9:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def _calc_atr(parsed_candles: list, period: int = 14, default_atr: float = 220.0) -> float:
    if len(parsed_candles) < 2:
        return default_atr
    trs = []
    for i in range(1, len(parsed_candles)):
        h, l, prev_c = parsed_candles[i][1], parsed_candles[i][2], parsed_candles[i - 1][3]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)

def _calc_adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    n = len(closes)
    if n < period * 2:
        return 25.0
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        plus_dm.append(h_diff if (h_diff > l_diff and h_diff > 0) else 0.0)
        minus_dm.append(l_diff if (l_diff > h_diff and l_diff > 0) else 0.0)
    tr_smooth = sum(tr[:period])
    plus_smooth = sum(plus_dm[:period])
    minus_smooth = sum(minus_dm[:period])
    dx = []
    for i in range(period, len(tr)):
        tr_smooth = tr_smooth - (tr_smooth / period) + tr[i]
        plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[i]
        minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[i]
        di_plus = (plus_smooth / (tr_smooth + 1e-9)) * 100.0
        di_minus = (minus_smooth / (tr_smooth + 1e-9)) * 100.0
        dx.append((abs(di_plus - di_minus) / (di_plus + di_minus + 1e-9)) * 100.0)
    if len(dx) < period:
        return sum(dx) / max(1, len(dx))
    adx = sum(dx[:period]) / period
    for d in dx[period:]:
        adx = (adx * (period - 1) + d) / period
    return round(float(adx), 1)

def _detect_fvg_and_displacement(closed_15: list, atr: float, min_gap: float) -> tuple:
    if len(closed_15) < 6:
        return False, False
    bull_fvg = False
    bear_fvg = False
    gap_thresh = max(min_gap * 0.35, atr * 0.12)

    for i in range(len(closed_15) - 4, len(closed_15)):
        c_minus2 = closed_15[i - 2]
        c_minus1 = closed_15[i - 1]
        c_cur = closed_15[i]
        if (c_cur[2] - c_minus2[1]) >= gap_thresh and (c_minus1[3] > c_minus1[0]):
            bull_fvg = True
        if (c_minus2[2] - c_cur[1]) >= gap_thresh and (c_minus1[3] < c_minus1[0]):
            bear_fvg = True

    last_bar = closed_15[-1]
    last_body = abs(last_bar[3] - last_bar[0])
    if last_bar[3] > last_bar[0] and last_body >= (atr * 0.65):
        bull_fvg = True
    if last_bar[3] < last_bar[0] and last_body >= (atr * 0.65):
        bear_fvg = True

    return bull_fvg, bear_fvg

def get_structure(market_dict: dict, symbol: str = "BTCUSDT") -> dict:
    klines = market_dict.get("klines", {})
    k15 = klines.get("15m", [])
    k1h = klines.get("1h", [])
    k4h = klines.get("4h", [])
    if len(k15) < 35:
        return {}

    asset_params = config.get_asset_params(symbol)
    p15 = [_parse_candle(c) for c in k15]
    closed_15 = p15[:-1]
    highs = [p[1] for p in closed_15]
    lows = [p[2] for p in closed_15]
    closes = [p[3] for p in closed_15]
    vols = [p[4] for p in closed_15]

    cur_close = closes[-1]
    atr_15m = _calc_atr(closed_15, 14, default_atr=asset_params["default_atr"])
    rsi_15m = _calc_rsi(closes, 14)
    adx_15m = _calc_adx(highs, lows, closes, 14)
    vol_sma20 = sum(vols[-20:]) / min(len(vols), 20)

    ema9 = _calc_ema(closes, 9)
    ema20 = _calc_ema(closes, 20)
    ema50 = _calc_ema(closes, 50)
    ema200 = _calc_ema(closes, min(len(closes), 200))

    bb_mid = sum(closes[-20:]) / 20.0
    bb_std = (sum((x - bb_mid) ** 2 for x in closes[-20:]) / 20.0) ** 0.5
    bb_bandwidth = (4.0 * bb_std) / (bb_mid + 1e-9)

    swing_lookback_bars = getattr(config, "SWING_LOOKBACK_BARS", 96)
    swing_lookback = min(len(closed_15) - 4, swing_lookback_bars)
    swing_high = max(highs[-swing_lookback - 3 : -3])
    swing_low = min(lows[-swing_lookback - 3 : -3])

    pdh = market_dict.get("pdh")
    pdl = market_dict.get("pdl")
    daily_open = market_dict.get("daily_open")
    day_high_24h = market_dict.get("day_high_24h")
    day_low_24h = market_dict.get("day_low_24h")
    asian_high = market_dict.get("asian_high")
    asian_low = market_dict.get("asian_low")

    choch_status = "NONE"
    choch_level = None

    if len(highs) >= 20:
        structural_floor = min(lows[-17:-2])
        structural_ceiling = max(highs[-17:-2])
        if (cur_close < structural_floor) and (closes[-1] < closes[-2]):
            choch_status = "BEARISH_CHOCH"
            choch_level = structural_floor
        elif (cur_close > structural_ceiling) and (closes[-1] > closes[-2]):
            choch_status = "BULLISH_CHOCH"
            choch_level = structural_ceiling

    bull_score = 0
    bear_score = 0

    if cur_close > ema200:
        bull_score += 1
    else:
        bear_score += 1

    if len(k1h) >= 15:
        p1h = [_parse_candle(c) for c in k1h[:-1]]
        closes_1h = [p[3] for p in p1h]
        ema50_1h = _calc_ema(closes_1h, 50)
        ema200_1h = _calc_ema(closes_1h, min(len(closes_1h), 200))
        cur_1h = closes_1h[-1]
        if cur_1h > ema200_1h:
            bull_score += 2
        else:
            bear_score += 2
        if ema50_1h >= ema200_1h * 0.998:
            bull_score += 1
        elif ema50_1h <= ema200_1h * 1.002:
            bear_score += 1

    htf_4h_bias = "NEUTRAL"
    if len(k4h) >= 10:
        p4h = [_parse_candle(c) for c in k4h[:-1]]
        closes_4h = [p[3] for p in p4h]
        ema50_4h = _calc_ema(closes_4h, min(len(closes_4h), 50))
        ema200_4h = _calc_ema(closes_4h, min(len(closes_4h), 200))
        cur_4h = closes_4h[-1]
        if cur_4h > ema50_4h and ema50_4h >= ema200_4h * 0.995:
            bull_score += 2
            htf_4h_bias = "BULLISH"
        elif cur_4h < ema50_4h and ema50_4h <= ema200_4h * 1.005:
            bear_score += 2
            htf_4h_bias = "BEARISH"

    if daily_open and daily_open > 0:
        if cur_close > daily_open:
            bull_score += 2
        else:
            bear_score += 2

    if bull_score >= 5 and bull_score > bear_score:
        macro_bias = "BULLISH"
    elif bear_score >= 5 and bear_score > bull_score:
        macro_bias = "BEARISH"
    elif bull_score >= 4 and bear_score <= 2:
        macro_bias = "BULLISH"
    elif bear_score >= 4 and bull_score <= 2:
        macro_bias = "BEARISH"
    else:
        macro_bias = "NEUTRAL"

    bull_fvg, bear_fvg = _detect_fvg_and_displacement(closed_15, atr_15m, asset_params["min_gap"])

    ema_spread_pct = abs(ema20 - ema50) / ema50
    is_choppy = (adx_15m < 20.0) and ((bb_bandwidth < 0.004) or (ema_spread_pct < 0.0015))

    return {
        "symbol": symbol,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "pdh": pdh,
        "pdl": pdl,
        "daily_open": daily_open,
        "day_high_24h": day_high_24h,
        "day_low_24h": day_low_24h,
        "asian_high": asian_high,
        "asian_low": asian_low,
        "current_close": cur_close,
        "ema9": ema9,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi_15m,
        "adx": adx_15m,
        "bb_bandwidth": bb_bandwidth,
        "vol_sma20": vol_sma20,
        "atr": atr_15m,
        "choch_status": choch_status,
        "choch_level": choch_level,
        "macro_bias": macro_bias,
        "htf_bias": macro_bias,
        "htf_4h_bias": htf_4h_bias,
        "bull_fvg": bull_fvg,
        "bear_fvg": bear_fvg,
        "is_choppy": is_choppy,
        "funding_rate": market_dict.get("funding_rate", 0.0001),
        "in_killzone": market_dict.get("in_killzone", False),
        "killzone_name": market_dict.get("killzone_name", "OFF_HOURS"),
        "is_dead_hour": market_dict.get("is_dead_hour", False),
        "news_blackout": market_dict.get("news_blackout", False),
        "cvd_ratio": market_dict.get("cvd_ratio", 0.50),
    }

def _btc_leader_allows(symbol: str, direction: str) -> bool:
    sym = symbol.strip().upper()
    if sym in ("BTCUSDT", "PAXGUSDT", "XAUUSD"):
        return True
    try:
        btc_m = market_data.get_market("BTCUSDT")
        if not btc_m or not btc_m.get("klines"):
            return True
        btc_cvd = float(btc_m.get("cvd_ratio") or 0.50)
        btc_p = float(btc_m.get("price") or 0.0)
        btc_d_open = float(btc_m.get("daily_open") or 0.0)
        if direction == "LONG":
            if btc_cvd < 0.42 and btc_d_open > 0 and btc_p < btc_d_open:
                return False
        elif direction == "SHORT":
            if btc_cvd > 0.58 and btc_d_open > 0 and btc_p > btc_d_open:
                return False
    except Exception:
        pass
    return True

def _build_trade_levels(direction: str, entry: float, raw_sl: float, atr: float, setup_name: str, score: float, candle_time: int, symbol: str = "BTCUSDT") -> dict:
    params = config.get_asset_params(symbol)
    sl_buffer = params["sl_buffer"]
    min_stop = params["min_stop"]
    max_stop = params["max_stop"]
    dec = int(params.get("decimals", 2))

    if direction == "LONG":
        structural_dist = (entry - raw_sl) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.7)))
        sl = round(entry - sl_dist, dec)
        tp1 = round(entry + (sl_dist * 1.4), dec)
        tp2 = round(entry + (sl_dist * 2.4), dec)
        tp3 = round(entry + (sl_dist * 3.5), dec)
    else:
        structural_dist = (raw_sl - entry) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.7)))
        sl = round(entry + sl_dist, dec)
        tp1 = round(entry - (sl_dist * 1.4), dec)
        tp2 = round(entry - (sl_dist * 2.4), dec)
        tp3 = round(entry - (sl_dist * 3.5), dec)

    return {
        "symbol": symbol,
        "direction": direction,
        "setup_name": setup_name,
        "score": round(score, 1),
        "entry": round(entry, dec),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "candle_time": candle_time
    }

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float, symbol: str = "BTCUSDT") -> dict:
    if len(candles_15m) < 35 or not structure:
        return None
    if structure.get("news_blackout", False):
        return None
    if getattr(config, "FILTER_DEAD_HOURS", True) and structure.get("is_dead_hour", False):
        return None
    if getattr(config, "STRICT_KILLZONE_ONLY", False) and not structure.get("in_killzone", False):
        return None

    params = config.get_asset_params(symbol)
    funding = structure.get("funding_rate", 0.0001)
    max_long_funding = getattr(config, "MAX_FUNDING_FOR_LONG", 0.00035)
    min_short_funding = getattr(config, "MIN_FUNDING_FOR_SHORT", -0.00020)

    is_choppy = structure.get("is_choppy", False)
    adx = structure.get("adx", 20.0)
    macro_bias = structure.get("macro_bias", "NEUTRAL")
    htf_4h_bias = structure.get("htf_4h_bias", "NEUTRAL")
    bull_fvg = structure.get("bull_fvg", False)
    bear_fvg = structure.get("bear_fvg", False)
    strict_trend_lock = getattr(config, "STRICT_TREND_LOCK", True)
    cvd_ratio = structure.get("cvd_ratio", 0.50)
    enable_cvd = getattr(config, "ENABLE_CVD_FILTER", True)

    parsed = [_parse_candle(c) for c in candles_15m]
    c = parsed[-2]
    prev = parsed[-3]
    o, h, l, cl, vol, c_time = c[0], c[1], c[2], c[3], c[4], c[5]
    prev_o, prev_h, prev_l, prev_cl = prev[0], prev[1], prev[2], prev[3]

    atr = structure.get("atr", params["default_atr"])
    sh = structure["swing_high"]
    sl = structure["swing_low"]
    pdh = structure.get("pdh")
    pdl = structure.get("pdl")
    day_high_24h = structure.get("day_high_24h")
    day_low_24h = structure.get("day_low_24h")

    ema9 = structure["ema9"]
    ema20 = structure["ema20"]
    ema50 = structure["ema50"]
    ema200 = structure["ema200"]
    rsi = structure["rsi"]
    vol_sma20 = structure.get("vol_sma20", 1.0)
    choch_status = structure.get("choch_status", "NONE")
    choch_level = structure.get("choch_level")

    if current_price and abs(current_price - cl) > (atr * 0.85):
        return None

    candle_range = max(1e-6, h - l)
    body_ratio = abs(cl - o) / candle_range
    lower_wick_ratio = (min(o, cl) - l) / candle_range
    upper_wick_ratio = (h - max(o, cl)) / candle_range

    recent_closed = parsed[-6:-1]
    lowest_recent = min(x[2] for x in recent_closed)
    highest_recent = max(x[1] for x in recent_closed)

    range_ceil = max([x for x in [pdh, day_high_24h, sh] if x and x > 0] or [cl + (atr * 3.0)])
    range_floor = min([x for x in [pdl, day_low_24h, sl] if x and x > 0] or [cl - (atr * 3.0)])
    range_height = max(atr * 2.0, range_ceil - range_floor)
    rel_pos = (cl - range_floor) / range_height

    enable_prem_disc = getattr(config, "ENABLE_PREMIUM_DISCOUNT_FILTER", True)
    in_discount = rel_pos < 0.50
    in_premium = rel_pos > 0.50
    room_to_floor = cl - range_floor
    room_to_ceil = range_ceil - cl
    min_boundary_buffer = atr * 1.25

    allow_long_cvd = (cvd_ratio >= 0.48) if enable_cvd else True
    allow_short_cvd = (cvd_ratio <= 0.52) if enable_cvd else True

    long_htf_ok = (htf_4h_bias != "BEARISH") and _btc_leader_allows(symbol, "LONG")
    short_htf_ok = (htf_4h_bias != "BULLISH") and _btc_leader_allows(symbol, "SHORT")
    vol_spike_ok = (vol >= vol_sma20 * 1.20)

    min_adx = getattr(config, "MIN_ADX_TREND", 22.0)
    enable_choch = getattr(config, "ENABLE_CHOCH_STRATEGY", True) and (not is_choppy) and (adx >= min_adx)

    if enable_choch and choch_status != "NONE" and choch_level:
        allow_short_choch = (macro_bias != "BULLISH") and short_htf_ok and ((not in_discount) and (room_to_floor >= min_boundary_buffer) if enable_prem_disc else True) and allow_short_cvd
        if choch_status == "BEARISH_CHOCH" and allow_short_choch and funding >= min_short_funding:
            if (cl < o) and (cl < choch_level) and (vol_spike_ok or bear_fvg) and (rsi <= 55.0):
                sl_anchor = max(highest_recent, choch_level + (atr * 0.20))
                return _build_trade_levels("SHORT", current_price or cl, sl_anchor, atr, "Sniper CHoCH + FVG Breakdown", 99.0, c_time, symbol)

        allow_long_choch = (macro_bias != "BEARISH") and long_htf_ok and ((not in_premium) and (room_to_ceil >= min_boundary_buffer) if enable_prem_disc else True) and allow_long_cvd
        if choch_status == "BULLISH_CHOCH" and allow_long_choch and funding <= max_long_funding:
            if (cl > o) and (cl > choch_level) and (vol_spike_ok or bull_fvg) and (rsi >= 45.0):
                sl_anchor = min(lowest_recent, choch_level - (atr * 0.20))
                return _build_trade_levels("LONG", current_price or cl, sl_anchor, atr, "Sniper CHoCH + FVG Breakout", 99.0, c_time, symbol)

    if getattr(config, "ENABLE_DAILY_BREAKOUT", True) and not is_choppy:
        breakout_high = pdh or day_high_24h
        if breakout_high and macro_bias == "BULLISH" and long_htf_ok and funding <= max_long_funding and allow_long_cvd:
            if (highest_recent >= breakout_high + (atr * 0.08)) and (cl > breakout_high) and (cl > o) and (body_ratio >= 0.48) and (upper_wick_ratio <= 0.28) and (vol_spike_ok or bull_fvg) and (rsi >= 50.0):
                return _build_trade_levels("LONG", current_price or cl, min(l, prev_l, ema20), atr, "Sniper Daily High Expansion", 98.0, c_time, symbol)

        breakdown_low = pdl or day_low_24h
        if breakdown_low and macro_bias == "BEARISH" and short_htf_ok and funding >= min_short_funding and allow_short_cvd:
            if (lowest_recent <= breakdown_low - (atr * 0.08)) and (cl < breakdown_low) and (cl < o) and (body_ratio >= 0.48) and (lower_wick_ratio <= 0.28) and (vol_spike_ok or bear_fvg) and (rsi <= 50.0):
                return _build_trade_levels("SHORT", current_price or cl, max(h, prev_h, ema20), atr, "Sniper Daily Low Breakdown", 98.0, c_time, symbol)

    daily_low_target = pdl or day_low_24h or structure.get("asian_low")
    allow_long_sweep = ((macro_bias != "BEARISH") if strict_trend_lock else True) and long_htf_ok
    if daily_low_target and allow_long_sweep and funding <= max_long_funding and allow_long_cvd:
        swept_daily_low = (lowest_recent <= daily_low_target + (atr * 0.03)) and (cl > daily_low_target)
        bullish_rejection = (cl > o) and (lower_wick_ratio >= 0.38 or body_ratio >= 0.48) and (rsi <= 48.0) and (vol >= vol_sma20 * 1.10 or bull_fvg)
        if swept_daily_low and bullish_rejection:
            return _build_trade_levels("LONG", current_price or cl, lowest_recent, atr, "Sniper PDH/PDL Liquidity Sweep", 97.0, c_time, symbol)

    daily_high_target = pdh or day_high_24h or structure.get("asian_high")
    allow_short_sweep = ((macro_bias != "BULLISH") if strict_trend_lock else True) and short_htf_ok
    if daily_high_target and allow_short_sweep and funding >= min_short_funding and allow_short_cvd:
        swept_daily_high = (highest_recent >= daily_high_target - (atr * 0.03)) and (cl < daily_high_target)
        bearish_rejection = (cl < o) and (upper_wick_ratio >= 0.38 or body_ratio >= 0.48) and (rsi >= 52.0) and (vol >= vol_sma20 * 1.10 or bear_fvg)
        if swept_daily_high and bearish_rejection:
            return _build_trade_levels("SHORT", current_price or cl, highest_recent, atr, "Sniper PDH/PDL Liquidity Sweep", 97.0, c_time, symbol)

    if allow_long_sweep and funding <= max_long_funding and in_discount and allow_long_cvd:
        if (lowest_recent <= sl - (atr * 0.06)) and (cl > sl) and (cl > o) and (lower_wick_ratio >= 0.38 or body_ratio >= 0.48) and (cl > prev_h or cl > ema9) and (rsi <= 48.0) and (vol_spike_ok or bull_fvg):
            return _build_trade_levels("LONG", current_price or cl, lowest_recent, atr, "24H Macro Swing Liquidity Trap", 95.0, c_time, symbol)

    if allow_short_sweep and funding >= min_short_funding and in_premium and allow_short_cvd:
        if (highest_recent >= sh + (atr * 0.06)) and (cl < sh) and (cl < o) and (upper_wick_ratio >= 0.38 or body_ratio >= 0.48) and (cl < prev_l or cl < ema9) and (rsi >= 52.0) and (vol_spike_ok or bear_fvg):
            return _build_trade_levels("SHORT", current_price or cl, highest_recent, atr, "24H Macro Swing Liquidity Trap", 95.0, c_time, symbol)

    if not is_choppy and adx >= min_adx:
        entry_candidate = current_price or cl
        trend_bullish = (macro_bias == "BULLISH") and long_htf_ok and (cl > ema200) and (ema9 > ema20 > ema50)
        touched_bull_zone = (l <= ema20 * 1.0015) and (cl >= ema20 * 0.999)
        strong_bull_candle = (cl > o) and (body_ratio >= 0.50) and (upper_wick_ratio <= 0.25) and (cl > prev_h) and (bull_fvg or vol >= vol_sma20 * 1.15)
        if trend_bullish and touched_bull_zone and strong_bull_candle and (48.0 <= rsi <= 68.0) and (funding <= max_long_funding) and allow_long_cvd:
            if not in_premium or not enable_prem_disc:
                overhead_res = [x for x in [pdh, day_high_24h, sh] if x and x > entry_candidate]
                headroom = (min(overhead_res) if overhead_res else 999999.0) - entry_candidate
                pullback_low = min(l, prev_l)
                est_sl_dist = max(params["min_stop"], (entry_candidate - pullback_low) + params["sl_buffer"])
                if headroom >= (est_sl_dist * 1.20):
                    return _build_trade_levels("LONG", entry_candidate, pullback_low, atr, "Sniper EMA + FVG Trend Pullback", 94.0, c_time, symbol)

        trend_bearish = (macro_bias == "BEARISH") and short_htf_ok and (cl < ema200) and (ema9 < ema20 < ema50)
        touched_bear_zone = (h >= ema20 * 0.9985) and (cl <= ema20 * 1.001)
        strong_bear_candle = (cl < o) and (body_ratio >= 0.50) and (lower_wick_ratio <= 0.25) and (cl < prev_l) and (bear_fvg or vol >= vol_sma20 * 1.15)
        if trend_bearish and touched_bear_zone and strong_bear_candle and (32.0 <= rsi <= 52.0) and (funding >= min_short_funding) and allow_short_cvd:
            if not in_discount or not enable_prem_disc:
                underlying_sup = [x for x in [pdl, day_low_24h, sl] if x and x < entry_candidate]
                headroom = entry_candidate - (max(underlying_sup) if underlying_sup else 0.0)
                pullback_high = max(h, prev_h)
                est_sl_dist = max(params["min_stop"], (pullback_high - entry_candidate) + params["sl_buffer"])
                if headroom >= (est_sl_dist * 1.20):
                    return _build_trade_levels("SHORT", entry_candidate, pullback_high, atr, "Sniper EMA + FVG Trend Pullback", 94.0, c_time, symbol)

    return None
