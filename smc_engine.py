import numpy as np
import logging

log = logging.getLogger("SMCEngine")

def get_structure(klines: dict) -> dict:
    k15 = klines.get("15m", [])
    if len(k15) < 30:
        return {}
    
    highs = [float(c[2]) for c in k15]
    lows = [float(c[3]) for c in k15]
    closes = [float(c[4]) for c in k15]

    # Dynamic swing points
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
    """
    Detects powerful institutional displacement candles that break structure (CHoCH/BOS)
    Captures vertical moves without waiting for deep pullbacks, while filtering fakeouts.
    """
    if len(candles) < 15 or not structure:
        return None

    c = candles[-1] # Latest closed 15M candle
    prev_candles = candles[-7:-1]

    o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
    v = float(c[5]) if len(c) > 5 else 1.0

    candle_range = h - l
    if candle_range <= 0:
        return None

    body = abs(cl - o)
    body_ratio = body / candle_range

    # Volume filter: must be > 1.35x average of previous 6 candles
    avg_vol = np.mean([float(x[5]) for x in prev_candles]) if len(prev_candles[0]) > 5 else 1.0
    vol_surge = (v >= avg_vol * 1.35) or (avg_vol == 1.0)

    # Bullish Displacement Breakout (Long)
    sh = structure["recent_high"]
    if cl > sh and o < sh and cl > o and body_ratio >= 0.65 and vol_surge:
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
    if cl < sl_level and o > sl_level and cl < o and body_ratio >= 0.65 and vol_surge:
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

    # Priority 1: Check High-Volume Displacement Breakout (To never miss vertical runs)
    disp_signal = detect_displacement_breakout(candles_15m, structure)
    if disp_signal:
        return disp_signal

    c = candles_15m[-1]
    o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
    candle_range = h - l
    if candle_range <= 0:
        return None

    sh = structure["swing_high"]
    sl = structure["swing_low"]

    # Priority 2: Classic Liquidity Sweep & Reclaim
    # SSL Sweep (Long Reversal)
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

    # BSL Sweep (Short Reversal)
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

    # Priority 3: 15M FVG 50% CE Mitigation / Retest
    c0, c1, c2 = candles_15m[-3], candles_15m[-2], candles_15m[-1]
    # Bullish FVG (c0 high < c2 low)
    if float(c2[3]) > float(c0[2]):
        fvg_ce = (float(c2[3]) + float(c0[2])) / 2.0
        if l <= fvg_ce <= h and cl > fvg_ce:
            risk = cl - float(c0[2])
            if risk > 0:
                return {
                    "direction": "LONG",
                    "setup_name": "FVG CE Retest in Discount",
                    "score": 82.0,
                    "entry": cl,
                    "sl": round(float(c0[2]), 2),
                    "tp1": round(cl + (risk * 1.5), 2),
                    "tp2": round(cl + (risk * 2.5), 2),
                    "tp3": round(cl + (risk * 4.0), 2)
                }

    # Bearish FVG (c0 low > c2 high)
    if float(c0[3]) > float(c2[2]):
        fvg_ce = (float(c0[3]) + float(c2[2])) / 2.0
        if l <= fvg_ce <= h and cl < fvg_ce:
            risk = float(c0[3]) - cl
            if risk > 0:
                return {
                    "direction": "SHORT",
                    "setup_name": "FVG CE Retest in Premium",
                    "score": 82.0,
                    "entry": cl,
                    "sl": round(float(c0[3]), 2),
                    "tp1": round(cl - (risk * 1.5), 2),
                    "tp2": round(cl - (risk * 2.5), 2),
                    "tp3": round(cl - (risk * 4.0), 2)
                }

    return None
