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
    k15, k1h, k4h = klines.get("15m", []), klines.get("1h", []), klines.get("4h", [])
    if len(k15) < 65: return {}
    p15 = [_parse_candle(c) for c in k15]
    highs, lows, closes = [p[1] for p in p15], [p[2] for p in p15], [p[3] for p in p15]
    cur_close, atr_15m = closes[-1], _calc_atr(p15, 14)
    range_high, range_low = max(highs[-48:-1]), min(lows[-48:-1])
    dealing_range = max(range_high - range_low, atr_15m * 2.5)
    return {
        "swing_high": max(highs[-35:-5]), "swing_low": min(lows[-35:-5]),
        "premium_level": range_low + (dealing_range * 0.55), "discount_level": range_low + (dealing_range * 0.45),
        "current_close": cur_close, "ema20": _calc_ema(closes, 20), "ema50": _calc_ema(closes, 50),
        "atr": atr_15m, "bias": "BULLISH" if cur_close > _calc_ema(closes, 50) else "BEARISH"
    }

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float) -> dict:
    if len(candles_15m) < 65 or not structure: return None
    atr, sh, sl = structure.get("atr", 250.0), structure["swing_high"], structure["swing_low"]
    discount_level, premium_level = structure["discount_level"], structure["premium_level"]
    parsed = [_parse_candle(c) for c in candles_15m]
    c, prev1, prev2 = parsed[-1], parsed[-2], parsed[-3]
    o, h, l, cl = c[0], c[1], c[2], c[3]
    if current_price and abs(current_price - cl) > (atr * 0.75): return None
    if (h - l) <= (atr * 0.35) or (abs(cl - o) / (h - l)) < 0.45: return None
    
    recent_window = parsed[-5:-1]
    lowest_recent, highest_recent = min(x[2] for x in recent_window), max(x[1] for x in recent_window)

    if discount_level and cl <= discount_level and lowest_recent < sl:
        if cl > o and cl > sl:
            risk = cl - lowest_recent
            return {"direction": "LONG", "setup_name": "Liquidity Sweep Breakout", "score": 94.0, "entry": round(cl, 2), "sl": round(lowest_recent, 2), "tp1": round(cl + (risk * 1.5), 2), "tp2": round(cl + (risk * 2.5), 2), "tp3": round(cl + (risk * 4.0), 2)}
    return None
