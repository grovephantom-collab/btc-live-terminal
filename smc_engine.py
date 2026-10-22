import logging

log = logging.getLogger("SMCEngine")

def _parse_candle(c):
    if isinstance(c, dict):
        return (float(c.get("open", 0.0)), float(c.get("high", 0.0)), float(c.get("low", 0.0)), float(c.get("close", 0.0)), float(c.get("vol", 1.0)))
    return (float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]) if len(c) > 5 else 1.0)

def _calc_ema(values: list, period: int) -> float:
    if not values: return 0.0
    if len(values) < period: return sum(values) / len(values)
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]: ema = (v * k) + (ema * (1.0 - k))
    return ema

def _calc_atr(parsed_candles: list, period: int = 14) -> float:
    if len(parsed_candles) < 2: return 250.0
    trs = []
    for i in range(1, len(parsed_candles)):
        h, l, prev_c = parsed_candles[i][1], parsed_candles[i][2], parsed_candles[i - 1][3]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)

def get_structure(klines: dict) -> dict:
    k15 = klines.get("15m", [])
    if len(k15) < 30: return {}
    p15 = [_parse_candle(c) for c in k15]
    highs, lows, closes = [p[1] for p in p15], [p[2] for p in p15], [p[3] for p in p15]
    cur_close, atr_15m = closes[-1], _calc_atr(p15, 14)
    
    lookback_range = min(len(p15) - 1, 48)
    range_high, range_low = max(highs[-lookback_range:-1]), min(lows[-lookback_range:-1])
    dealing_range = max(range_high - range_low, atr_15m * 2.0)
    
    swing_lookback = min(len(p15) - 5, 35)
    return {
        "swing_high": max(highs[-swing_lookback:-3]), 
        "swing_low": min(lows[-swing_lookback:-3]),
        "premium_level": range_low + (dealing_range * 0.55), 
        "discount_level": range_low + (dealing_range * 0.45),
        "current_close": cur_close, 
        "ema20": _calc_ema(closes, 20), 
        "ema50": _calc_ema(closes, 50),
        "atr": atr_15m, 
        "bias": "BULLISH" if cur_close > _calc_ema(closes, 50) else "BEARISH"
    }

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float) -> dict:
    if len(candles_15m) < 30 or not structure: return None
    atr = structure.get("atr", 250.0)
    sh = structure["swing_high"]
    sl = structure["swing_low"]
    
    parsed = [_parse_candle(c) for c in candles_15m]
    c = parsed[-1]
    o, h, l, cl = c[0], c[1], c[2], c[3]
    
    if current_price and abs(current_price - cl) > (atr * 1.5): return None
    
    # Recent sweep window (last 6 candles)
    recent_window = parsed[-6:-1]
    if not recent_window: return None
    lowest_recent = min(x[2] for x in recent_window)
    highest_recent = max(x[1] for x in recent_window)

    candle_range = max(1.0, h - l)
    body_ratio = abs(cl - o) / candle_range

    # 1. SMC BULLISH LIQUIDITY SWEEP (LONG SETUP)
    if lowest_recent <= sl and cl > sl and cl > o:
        if body_ratio >= 0.35:
            risk = max(atr * 0.5, cl - lowest_recent)
            return {
                "direction": "LONG",
                "setup_name": "SMC Bullish Liquidity Sweep",
                "score": 92.0,
                "entry": round(cl, 2),
                "sl": round(lowest_recent - (atr * 0.1), 2),
                "tp1": round(cl + (risk * 1.5), 2),
                "tp2": round(cl + (risk * 2.5), 2),
                "tp3": round(cl + (risk * 4.0), 2)
            }

    # 2. SMC BEARISH LIQUIDITY SWEEP (SHORT SETUP)
    if highest_recent >= sh and cl < sh and cl < o:
        if body_ratio >= 0.35:
            risk = max(atr * 0.5, highest_recent - cl)
            return {
                "direction": "SHORT",
                "setup_name": "SMC Bearish Liquidity Sweep",
                "score": 92.0,
                "entry": round(cl, 2),
                "sl": round(highest_recent + (atr * 0.1), 2),
                "tp1": round(cl - (risk * 1.5), 2),
                "tp2": round(cl - (risk * 2.5), 2),
                "tp3": round(cl - (risk * 4.0), 2)
            }

    return None
