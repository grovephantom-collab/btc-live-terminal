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

def get_structure(klines: dict) -> dict:
    k15 = klines.get("15m", [])
    if len(k15) < 30:
        return {}
    
    parsed = [_parse_candle(c) for c in k15]
    highs = [p[1] for p in parsed]
    lows = [p[2] for p in parsed]
    closes = [p[3] for p in parsed]

    swing_high = max(highs[-25:-1])
    swing_low = min(lows[-25:-1])
    recent_high = max(highs[-10:-1])
    recent_low = min(lows[-10:-1])

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "current_close": closes[-1]
    }

def detect_displacement_breakout(candles: list, structure: dict) -> dict:
    if len(candles) < 15 or not structure:
        return None

    parsed_all = [_parse_candle(c) for c in candles]
    c = parsed_all[-1]
    prev_candles = parsed_all[-7:-1]

    o, h, l, cl, v = c[0], c[1], c[2], c[3], c[4]

    candle_range = h - l
    if candle_range <= 0:
        return None

    body = abs(cl - o)
    body_ratio = body / candle_range

    vols = [x[4] for x in prev_candles]
    avg_vol = (sum(vols) / len(vols)) if vols else 1.0
    vol_surge = (v >= avg_vol * 1.15) or (avg_vol == 1.0)

    # Bullish Displacement Breakout (Long)
    sh = structure["recent_high"]
    if cl > sh and cl > o and body_ratio >= 0.55 and vol_surge:
        sl = max(l, cl - (candle_range * 0.70))
        risk = cl - sl
        if risk > 0:
            return {
                "direction": "LONG",
                "setup_name": "Displacement CHoCH/BOS Breakout",
                "score": 85.0,
                "entry": cl,
                "sl": round(sl, 2),
                "tp1": round(cl + (risk * 1.5), 2),
                "tp2": round(cl + (risk * 2.5), 2),
                "tp3": round(cl + (risk * 4.0), 2)
            }

    # Bearish Displacement Breakout (Short)
    sl_level = structure["recent_low"]
    if cl < sl_level and cl < o and body_ratio >= 0.55 and vol_surge:
        sl = min(h, cl + (candle_range * 0.70))
        risk = sl - cl
        if risk > 0:
            return {
                "direction": "SHORT",
                "setup_name": "Displacement CHoCH/BOS Breakout",
                "score": 85.0,
                "entry": cl,
                "sl": round(sl, 2),
                "tp1": round(cl - (risk * 1.5), 2),
                "tp2": round(cl - (risk * 2.5), 2),
                "tp3": round(cl - (risk * 4.0), 2)
            }

    return None

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float) -> dict:
    if len(candles_15m) < 20 or not structure:
        return None

    disp_signal = detect_displacement_breakout(candles_15m, structure)
    if disp_signal:
        return disp_signal

    parsed = [_parse_candle(c) for c in candles_15m]
    c = parsed[-1]
    o, h, l, cl, _ = c[0], c[1], c[2], c[3], c[4]
    candle_range = h - l
    if candle_range <= 0:
        return None

    sh = structure["swing_high"]
    sl = structure["swing_low"]

    if l < sl and cl > sl:
        lower_wick = min(o, cl) - l
        if (lower_wick / candle_range) >= 0.35:
            risk = cl - l
            if risk > 0:
                return {
                    "direction": "LONG",
                    "setup_name": "SSL Sweep & Reclaim",
                    "score": 88.0,
                    "entry": cl,
                    "sl": round(l - (candle_range * 0.1), 2),
                    "tp1": round(cl + (risk * 1.5), 2),
                    "tp2": round(cl + (risk * 2.5), 2),
                    "tp3": round(cl + (risk * 4.0), 2)
                }

    if h > sh and cl < sh:
        upper_wick = h - max(o, cl)
        if (upper_wick / candle_range) >= 0.35:
            risk = h - cl
            if risk > 0:
                return {
                    "direction": "SHORT",
                    "setup_name": "BSL Sweep & Reclaim",
                    "score": 88.0,
                    "entry": cl,
                    "sl": round(h + (candle_range * 0.1), 2),
                    "tp1": round(cl - (risk * 1.5), 2),
                    "tp2": round(cl - (risk * 2.5), 2),
                    "tp3": round(cl - (risk * 4.0), 2)
                }

    c0, c1, c2 = parsed[-3], parsed[-2], parsed[-1]
    if c2[2] > c0[1]:
        fvg_ce = (c2[2] + c0[1]) / 2.0
        if l <= fvg_ce <= h and cl > fvg_ce:
            risk = cl - c0[1]
            if risk > 0:
                return {
                    "direction": "LONG",
                    "setup_name": "FVG CE Retest in Discount",
                    "score": 82.0,
                    "entry": cl,
                    "sl": round(c0[1], 2),
                    "tp1": round(cl + (risk * 1.5), 2),
                    "tp2": round(cl + (risk * 2.5), 2),
                    "tp3": round(cl + (risk * 4.0), 2)
                }

    if c0[2] > c2[1]:
        fvg_ce = (c0[2] + c2[1]) / 2.0
        if l <= fvg_ce <= h and cl < fvg_ce:
            risk = c0[2] - cl
            if risk > 0:
                return {
                    "direction": "SHORT",
                    "setup_name": "FVG CE Retest in Premium",
                    "score": 82.0,
                    "entry": cl,
                    "sl": round(c0[2], 2),
                    "tp1": round(cl - (risk * 1.5), 2),
                    "tp2": round(cl - (risk * 2.5), 2),
                    "tp3": round(cl - (risk * 4.0), 2)
                }

    return None
