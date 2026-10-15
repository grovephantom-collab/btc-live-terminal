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
    if len(k15) < 65:
        return {}

    p15 = [_parse_candle(c) for c in k15]
    highs = [p[1] for p in p15]
    lows = [p[2] for p in p15]
    closes = [p[3] for p in p15]

    cur_close = closes[-1]
    ema20_15m = _calc_ema(closes, 20)
    ema50_15m = _calc_ema(closes, 50)
    atr_15m = _calc_atr(p15, 14)

    # Dealing Range (48 candles / 12 hours) for Premium & Discount Zones
    range_high = max(highs[-48:-1])
    range_low = min(lows[-48:-1])
    dealing_range = max(range_high - range_low, atr_15m * 2.5)

    premium_level = range_low + (dealing_range * 0.55)
    discount_level = range_low + (dealing_range * 0.45)

    # Macro Top-Down Trend Bias (1H + 4H Confluence)
    htf_bias = "NEUTRAL"
    if len(k1h) >= 30:
        p1h = [_parse_candle(c) for c in k1h]
        c1h = [p[3] for p in p1h]
        ema20_1h = _calc_ema(c1h, 20)
        ema50_1h = _calc_ema(c1h, 50)
        if cur_close > ema20_1h and ema20_1h >= ema50_1h:
            htf_bias = "BULLISH"
        elif cur_close < ema20_1h and ema20_1h <= ema50_1h:
            htf_bias = "BEARISH"
        else:
            htf_bias = "BULLISH" if cur_close > ema50_1h else "BEARISH"
    else:
        htf_bias = "BULLISH" if cur_close > ema50_15m else "BEARISH"

    if len(k4h) >= 15:
        p4h = [_parse_candle(c) for c in k4h]
        c4h = [p[3] for p in p4h]
        ema20_4h = _calc_ema(c4h, 20)
        if cur_close < ema20_4h and cur_close < ema50_15m:
            htf_bias = "BEARISH"
        elif cur_close > ema20_4h and cur_close > ema50_15m:
            htf_bias = "BULLISH"

    return {
        "swing_high": max(highs[-35:-5]),
        "swing_low": min(lows[-35:-5]),
        "premium_level": premium_level,
        "discount_level": discount_level,
        "current_close": cur_close,
        "ema20": ema20_15m,
        "ema50": ema50_15m,
        "atr": atr_15m,
        "bias": htf_bias
    }

def _build_trade(direction: str, setup_name: str, score: float, entry: float, structural_sl: float, atr: float) -> dict:
    min_dist = max(atr * 1.5, entry * 0.0055)
    max_dist = entry * 0.020

    if direction == "LONG":
        sl = min(structural_sl - (atr * 0.25), entry - min_dist)
        risk = entry - sl
        if risk <= 0 or risk > max_dist:
            return None
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
        sl = max(structural_sl + (atr * 0.25), entry + min_dist)
        risk = sl - entry
        if risk <= 0 or risk > max_dist:
            return None
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

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float) -> dict:
    if len(candles_15m) < 65 or not structure:
        return None

    bias = structure.get("bias", "NEUTRAL")
    atr = structure.get("atr", 250.0)
    sh = structure["swing_high"]
    sl = structure["swing_low"]
    premium_level = structure["premium_level"]
    discount_level = structure["discount_level"]

    parsed = [_parse_candle(c) for c in candles_15m]
    c = parsed[-1]      # Current closed candle
    prev1 = parsed[-2]  # 1 candle ago
    prev2 = parsed[-3]  # 2 candles ago
    o, h, l, cl, v = c[0], c[1], c[2], c[3], c[4]

    if current_price and abs(current_price - cl) > (atr * 0.75):
        return None

    candle_range = h - l
    body = abs(cl - o)
    if candle_range <= (atr * 0.35) or (body / candle_range) < 0.45:
        return None

    recent_window = parsed[-5:-1]
    lowest_recent = min(x[2] for x in recent_window)
    highest_recent = max(x[1] for x in recent_window)

    # =========================================================================
    # SETUP 1: MULTI-CANDLE LIQUIDITY SWEEP + BASE HOLD + BREAKOUT CONFIRMATION
    # =========================================================================
    if discount_level and cl <= discount_level and lowest_recent < sl:
        holding_candles = sum(1 for x in recent_window if x[3] > sl and x[2] >= lowest_recent)
        trigger_high = max(prev1[1], prev2[1])
        if holding_candles >= 2 and cl > o and cl > trigger_high and cl > sl:
            return _build_trade(
                "LONG", "Liquidity Sweep + Base Hold Breakout", 94.0, cl, lowest_recent, atr
            )

    if premium_level and cl >= premium_level and highest_recent > sh:
        holding_candles = sum(1 for x in recent_window if x[3] < sh and x[1] <= highest_recent)
        trigger_low = min(prev1[2], prev2[2])
        if holding_candles >= 2 and cl < o and cl < trigger_low and cl < sh:
            return _build_trade(
                "SHORT", "Liquidity Sweep + Base Hold Breakdown", 94.0, cl, highest_recent, atr
            )

    # =========================================================================
    # SETUP 2: MARKET STRUCTURE SHIFT (MSS) + UNMITIGATED FVG MITIGATION
    # =========================================================================
    if bias == "BULLISH" and cl >= discount_level:
        # Scan for Bullish FVG formed after a sweep
        for idx in range(3, 25):
            c_first = parsed[-(idx + 2)]
            c_third = parsed[-idx]
            fvg_bot, fvg_top = c_first[1], c_third[2]
            gap_size = fvg_top - fvg_bot
            if gap_size >= (atr * 0.35):
                intervening = parsed[-(idx - 1):-1]
                already_mitigated = any(x[2] <= ((fvg_top + fvg_bot) / 2.0) for x in intervening)
                if not already_mitigated:
                    fvg_ce = (fvg_top + fvg_bot) / 2.0
                    if l <= fvg_ce and cl > o and cl > fvg_ce:
                        return _build_trade(
                            "LONG", "MSS + Bullish FVG Mitigation", 91.0, cl, c_first[2], atr
                        )

    elif bias == "BEARISH" and cl <= premium_level:
        # Scan for Bearish FVG formed after a sweep
        for idx in range(3, 25):
            c_first = parsed[-(idx + 2)]
            c_third = parsed[-idx]
            fvg_top, fvg_bot = c_first[2], c_third[1]
            gap_size = fvg_top - fvg_bot
            if gap_size >= (atr * 0.35):
                intervening = parsed[-(idx - 1):-1]
                already_mitigated = any(x[1] >= ((fvg_top + fvg_bot) / 2.0) for x in intervening)
                if not already_mitigated:
                    fvg_ce = (fvg_top + fvg_bot) / 2.0
                    if h >= fvg_ce and cl < o and cl < fvg_ce:
                        return _build_trade(
                            "SHORT", "MSS + Bearish FVG Mitigation", 91.0, cl, c_first[1], atr
                        )

    return None
