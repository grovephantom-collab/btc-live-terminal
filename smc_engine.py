import logging

log = logging.getLogger("SMCEngine")

def _parse_candle(c):
    if isinstance(c, dict):
        return (
            float(c.get("open", 0.0)),
            float(c.get("high", 0.0)),
            float(c.get("low", 0.0)),
            float(c.get("close", 0.0)),
            float(c.get("vol", 1.0))
        )
    return (
        float(c[1]),
        float(c[2]),
        float(c[3]),
        float(c[4]),
        float(c[5]) if len(c) > 5 else 1.0
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

def _calc_atr(parsed_candles: list, period: int = 14) -> float:
    if len(parsed_candles) < 2:
        return 250.0
    trs = []
    for i in range(1, len(parsed_candles)):
        h = parsed_candles[i][1]
        l = parsed_candles[i][2]
        prev_c = parsed_candles[i - 1][3]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)

def get_structure(klines: dict) -> dict:
    k15 = klines.get("15m", [])
    k1h = klines.get("1h", [])
    k4h = klines.get("4h", [])
    if len(k15) < 50:
        return {}

    p15 = [_parse_candle(c) for c in k15]
    highs = [p[1] for p in p15]
    lows = [p[2] for p in p15]
    closes = [p[3] for p in p15]

    cur_close = closes[-1]
    ema50_15m = _calc_ema(closes, 50)
    ema20_15m = _calc_ema(closes, 20)
    atr_15m = _calc_atr(p15, 14)

    # Higher Timeframe (1H & 4H) Trend Bias Detection
    htf_bias = "NEUTRAL"
    if len(k1h) >= 25:
        p1h = [_parse_candle(c) for c in k1h]
        c1h = [p[3] for p in p1h]
        ema20_1h = _calc_ema(c1h, 20)
        ema50_1h = _calc_ema(c1h, 50)
        # Strong Bearish if price < 1H EMA20 and 15m < EMA50
        if cur_close < ema20_1h and cur_close < ema50_15m and ema20_15m < ema50_15m:
            htf_bias = "BEARISH"
        # Strong Bullish if price > 1H EMA20 and 15m > EMA50
        elif cur_close > ema20_1h and cur_close > ema50_15m and ema20_15m > ema50_15m:
            htf_bias = "BULLISH"
    else:
        if cur_close < ema50_15m and ema20_15m < ema50_15m:
            htf_bias = "BEARISH"
        elif cur_close > ema50_15m and ema20_15m > ema50_15m:
            htf_bias = "BULLISH"

    # Check 4H structure to prevent catching falling knives
    if len(k4h) >= 10:
        p4h = [_parse_candle(c) for c in k4h]
        c4h = [p[3] for p in p4h]
        ema20_4h = _calc_ema(c4h, 20)
        if cur_close < ema20_4h and cur_close < ema50_15m:
            htf_bias = "BEARISH"
        elif cur_close > ema20_4h and cur_close > ema50_15m:
            htf_bias = "BULLISH"

    lookback_48h = min(len(highs) - 1, 192)

    return {
        "swing_high": max(highs[-32:-1]),
        "swing_low": min(lows[-32:-1]),
        "recent_high": max(highs[-12:-1]),
        "recent_low": min(lows[-12:-1]),
        "major_high": max(highs[-lookback_48h:-1]),
        "major_low": min(lows[-lookback_48h:-1]),
        "current_close": cur_close,
        "ema20": ema20_15m,
        "ema50": ema50_15m,
        "atr": atr_15m,
        "bias": htf_bias
    }

def _build_trade(direction: str, setup_name: str, score: float, entry: float, raw_sl: float, atr: float) -> dict:
    # Enforce Institutional Stop Distance: Minimum 1.8x ATR or 0.60% of BTC price (prevents $170 scalping stops)
    min_stop_dist = max(atr * 1.8, entry * 0.0060)
    max_stop_dist = entry * 0.022  # Cap max stop at 2.2%

    if direction == "LONG":
        actual_dist = max(entry - raw_sl, min_stop_dist)
        if actual_dist > max_stop_dist:
            return None
        sl = entry - actual_dist
        risk = entry - sl
        return {
            "direction": "LONG",
            "setup_name": setup_name,
            "score": score,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(entry + (risk * 1.5), 2),
            "tp2": round(entry + (risk * 2.5), 2),
            "tp3": round(entry + (risk * 4.0), 2)
        }
    else:
        actual_dist = max(raw_sl - entry, min_stop_dist)
        if actual_dist > max_stop_dist:
            return None
        sl = entry + actual_dist
        risk = sl - entry
        return {
            "direction": "SHORT",
            "setup_name": setup_name,
            "score": score,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(entry - (risk * 1.5), 2),
            "tp2": round(entry - (risk * 2.5), 2),
            "tp3": round(entry - (risk * 4.0), 2)
        }

def detect_displacement_breakout(candles: list, structure: dict) -> dict:
    if len(candles) < 30 or not structure:
        return None

    bias = structure.get("bias", "NEUTRAL")
    atr = structure.get("atr", 250.0)

    parsed_all = [_parse_candle(c) for c in candles]
    c = parsed_all[-1]
    prev_candles = parsed_all[-10:-1]

    o, h, l, cl, v = c[0], c[1], c[2], c[3], c[4]
    candle_range = h - l
    if candle_range <= (atr * 0.6):
        return None

    body = abs(cl - o)
    body_ratio = body / candle_range

    vols = [x[4] for x in prev_candles]
    avg_vol = (sum(vols) / len(vols)) if vols else 1.0
    vol_surge = (v >= avg_vol * 1.25)

    # Bullish Breakout: ONLY allowed when HTF/MTF Bias is BULLISH
    sh = structure["swing_high"]
    if bias == "BULLISH" and cl > sh and cl > o and body_ratio >= 0.60 and vol_surge:
        raw_sl = min(l, structure["recent_low"])
        return _build_trade("LONG", "Trend BOS Displacement Breakout", 89.0, cl, raw_sl, atr)

    # Bearish Breakdown: ONLY allowed when HTF/MTF Bias is BEARISH
    sl_level = structure["swing_low"]
    if bias == "BEARISH" and cl < sl_level and cl < o and body_ratio >= 0.60 and vol_surge:
        raw_sl = max(h, structure["recent_high"])
        return _build_trade("SHORT", "Trend BOS Displacement Breakdown", 89.0, cl, raw_sl, atr)

    return None

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float) -> dict:
    if len(candles_15m) < 50 or not structure:
        return None

    bias = structure.get("bias", "NEUTRAL")
    atr = structure.get("atr", 250.0)
    parsed = [_parse_candle(c) for c in candles_15m]
    c = parsed[-1]
    prev = parsed[-2]
    o, h, l, cl, _ = c[0], c[1], c[2], c[3], c[4]

    # Guard: If live market price has already moved far away from closed candle, skip stale signal
    if current_price and abs(current_price - cl) > (atr * 0.8):
        return None

    # 1. Check Trend-Aligned Displacement BOS first
    disp_signal = detect_displacement_breakout(candles_15m, structure)
    if disp_signal:
        return disp_signal

    candle_range = h - l
    if candle_range <= 0:
        return None

    sh = structure["swing_high"]
    sl = structure["swing_low"]

    # 2. Bullish Setups: STRICTLY ALLOWED ONLY IN BULLISH BIAS
    if bias == "BULLISH":
        # SSL Sweep & Bullish Reclaim (Must be a green candle closing above previous candle's high/body)
        if l < sl and cl > sl and cl > o and cl > prev[3]:
            lower_wick = o - l
            if (lower_wick / candle_range) >= 0.40:
                return _build_trade("LONG", "HTF-Aligned SSL Sweep & Reclaim", 90.0, cl, l - (atr * 0.3), atr)

        # Bullish FVG CE Retest in Uptrend (Must reject with a green bullish candle)
        for idx in range(2, 6):
            c_first = parsed[-(idx + 2)]
            c_third = parsed[-idx]
            if (c_third[2] - c_first[1]) > (atr * 0.25):  # Meaningful gap size
                fvg_ce = (c_third[2] + c_first[1]) / 2.0
                if l <= fvg_ce <= h and cl > fvg_ce and cl > o:
                    raw_sl = min(l, c_first[2])
                    return _build_trade("LONG", "Bullish Trend FVG Retest", 86.0, cl, raw_sl, atr)

    # 3. Bearish Setups: STRICTLY ALLOWED ONLY IN BEARISH BIAS
    elif bias == "BEARISH":
        # BSL Sweep & Bearish Reclaim (Must be a red candle closing below previous candle's low/body)
        if h > sh and cl < sh and cl < o and cl < prev[3]:
            upper_wick = h - o
            if (upper_wick / candle_range) >= 0.40:
                return _build_trade("SHORT", "HTF-Aligned BSL Sweep & Reclaim", 90.0, cl, h + (atr * 0.3), atr)

        # Bearish FVG CE Retest in Downtrend (Must reject with a red bearish candle)
        for idx in range(2, 6):
            c_first = parsed[-(idx + 2)]
            c_third = parsed[-idx]
            if (c_first[2] - c_third[1]) > (atr * 0.25):  # Meaningful gap size
                fvg_ce = (c_first[2] + c_third[1]) / 2.0
                if l <= fvg_ce <= h and cl < fvg_ce and cl < o:
                    raw_sl = max(h, c_first[1])
                    return _build_trade("SHORT", "Bearish Trend FVG Retest", 86.0, cl, raw_sl, atr)

    return None
