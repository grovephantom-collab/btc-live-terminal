import logging
import config

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

def _calc_atr(parsed_candles: list, period: int = 14) -> float:
    if len(parsed_candles) < 2:
        return 220.0
    trs = []
    for i in range(1, len(parsed_candles)):
        h, l, prev_c = parsed_candles[i][1], parsed_candles[i][2], parsed_candles[i - 1][3]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)

def _detect_fvg_zones(parsed_candles: list, min_gap: float = 25.0) -> dict:
    bull_fvg, bear_fvg = None, None
    n = len(parsed_candles)
    if n < 6:
        return {"bull_fvg": None, "bear_fvg": None}
    for i in range(max(2, n - 20), n - 1):
        c_low = parsed_candles[i][2]
        prev2_high = parsed_candles[i - 2][1]
        if c_low - prev2_high >= min_gap:
            bull_fvg = {"bottom": prev2_high, "top": c_low, "ce": (c_low + prev2_high) / 2.0}
        c_high = parsed_candles[i][1]
        prev2_low = parsed_candles[i - 2][2]
        if prev2_low - c_high >= min_gap:
            bear_fvg = {"top": prev2_low, "bottom": c_high, "ce": (prev2_low + c_high) / 2.0}
    return {"bull_fvg": bull_fvg, "bear_fvg": bear_fvg}

def get_structure(klines: dict) -> dict:
    k15 = klines.get("15m", [])
    k1h = klines.get("1h", [])
    if len(k15) < 35:
        return {}

    p15 = [_parse_candle(c) for c in k15]
    closed_15 = p15[:-1]
    highs = [p[1] for p in closed_15]
    lows = [p[2] for p in closed_15]
    closes = [p[3] for p in closed_15]
    vols = [p[4] for p in closed_15]

    cur_close = closes[-1]
    atr_15m = _calc_atr(closed_15, 14)
    rsi_15m = _calc_rsi(closes, 14)
    vol_sma20 = sum(vols[-20:]) / min(len(vols), 20)

    ema9 = _calc_ema(closes, 9)
    ema20 = _calc_ema(closes, 20)
    ema50 = _calc_ema(closes, 50)
    ema200 = _calc_ema(closes, min(len(closes), 200))

    # 1H Higher Timeframe (HTF) Bias (Stock Learner Multi-TF Rule)
    htf_bias = "NEUTRAL"
    htf_swing_high, htf_swing_low = max(highs[-32:]), min(lows[-32:])
    if len(k1h) >= 25:
        p1h = [_parse_candle(c) for c in k1h[:-1]] if len(k1h) > 25 else [_parse_candle(c) for c in k1h]
        closes_1h = [p[3] for p in p1h]
        highs_1h = [p[1] for p in p1h]
        lows_1h = [p[2] for p in p1h]
        ema20_1h = _calc_ema(closes_1h, 20)
        ema50_1h = _calc_ema(closes_1h, 50)
        htf_swing_high = max(highs_1h[-16:])
        htf_swing_low = min(lows_1h[-16:])
        if closes_1h[-1] > ema50_1h and ema20_1h >= ema50_1h * 0.998:
            htf_bias = "BULLISH"
        elif closes_1h[-1] < ema50_1h and ema20_1h <= ema50_1h * 1.002:
            htf_bias = "BEARISH"
    else:
        htf_bias = "BULLISH" if cur_close > ema50 else "BEARISH"

    lookback_range = min(len(closed_15) - 1, 48)
    range_high = max(highs[-lookback_range:])
    range_low = min(lows[-lookback_range:])
    dealing_range = max(range_high - range_low, atr_15m * 2.0)

    swing_lookback = min(len(closed_15) - 4, 30)
    swing_high = max(highs[-swing_lookback - 3 : -3])
    swing_low = min(lows[-swing_lookback - 3 : -3])

    fvgs = _detect_fvg_zones(closed_15, min_gap=getattr(config, "MIN_GAP", 30.0))

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "htf_swing_high": htf_swing_high,
        "htf_swing_low": htf_swing_low,
        "premium_level": range_low + (dealing_range * 0.55),
        "discount_level": range_low + (dealing_range * 0.45),
        "current_close": cur_close,
        "ema9": ema9,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi_15m,
        "vol_sma20": vol_sma20,
        "atr": atr_15m,
        "bias": "BULLISH" if (cur_close > ema50 and ema20 >= ema50) else ("BEARISH" if (cur_close < ema50 and ema20 <= ema50) else htf_bias),
        "htf_bias": htf_bias,
        "bull_fvg": fvgs["bull_fvg"],
        "bear_fvg": fvgs["bear_fvg"]
    }

def _build_trade_levels(direction: str, entry: float, raw_sl: float, atr: float, setup_name: str, score: float, candle_time: int) -> dict:
    sl_buffer = getattr(config, "SL_BUFFER", 35.0)
    min_stop = getattr(config, "MIN_STOP", 140.0)
    max_stop = getattr(config, "MAX_STOP", 450.0)

    if direction == "LONG":
        structural_dist = (entry - raw_sl) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.1)))
        sl = round(entry - sl_dist, 2)
        tp1 = round(entry + (sl_dist * 1.5), 2)
        tp2 = round(entry + (sl_dist * 2.5), 2)
        tp3 = round(entry + (sl_dist * 3.5), 2)
    else:
        structural_dist = (raw_sl - entry) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.1)))
        sl = round(entry + sl_dist, 2)
        tp1 = round(entry - (sl_dist * 1.5), 2)
        tp2 = round(entry - (sl_dist * 2.5), 2)
        tp3 = round(entry - (sl_dist * 3.5), 2)

    return {
        "direction": direction,
        "setup_name": setup_name,
        "score": round(score, 1),
        "entry": round(entry, 2),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "candle_time": candle_time
    }

def evaluate_signal(structure: dict, candles_15m: list, current_price: float, session: str, poc: float) -> dict:
    if len(candles_15m) < 35 or not structure:
        return None

    parsed = [_parse_candle(c) for c in candles_15m]
    # Evaluate ONLY the last CONFIRMED CLOSED 15M candle (parsed[-2])
    c = parsed[-2]
    prev = parsed[-3]
    o, h, l, cl, vol, c_time = c[0], c[1], c[2], c[3], c[4], c[5]
    prev_o, prev_h, prev_l, prev_cl = prev[0], prev[1], prev[2], prev[3]

    atr = structure.get("atr", 220.0)
    sh = structure["swing_high"]
    sl = structure["swing_low"]
    ema9 = structure["ema9"]
    ema20 = structure["ema20"]
    ema50 = structure["ema50"]
    rsi = structure["rsi"]
    vol_sma20 = structure.get("vol_sma20", 1.0)
    htf_bias = structure.get("htf_bias", "NEUTRAL")

    if current_price and abs(current_price - cl) > (atr * 0.85):
        return None

    candle_range = max(1.0, h - l)
    body = abs(cl - o)
    body_ratio = body / candle_range
    lower_wick_ratio = (min(o, cl) - l) / candle_range
    upper_wick_ratio = (h - max(o, cl)) / candle_range

    recent_closed = parsed[-5:-1]
    lowest_recent = min(x[2] for x in recent_closed)
    highest_recent = max(x[1] for x in recent_closed)

    # 1. STOCK LEARNER (GAUTAM JHA) LIQUIDITY SWEEP + TRAP REVERSAL
    swept_low = (lowest_recent <= sl) and (cl > sl)
    bullish_reclaim = (cl > o) and (body_ratio >= 0.42 or lower_wick_ratio >= 0.40) and (cl > prev_h or cl > ema9)
    if swept_low and bullish_reclaim and (32.0 <= rsi <= 68.0):
        if htf_bias != "BEARISH" or vol >= vol_sma20 * 1.15:
            return _build_trade_levels(
                direction="LONG",
                entry=current_price or cl,
                raw_sl=lowest_recent,
                atr=atr,
                setup_name="Stock Learner Liquidity Sweep Reversal",
                score=94.0,
                candle_time=c_time
            )

    swept_high = (highest_recent >= sh) and (cl < sh)
    bearish_reclaim = (cl < o) and (body_ratio >= 0.42 or upper_wick_ratio >= 0.40) and (cl < prev_l or cl < ema9)
    if swept_high and bearish_reclaim and (32.0 <= rsi <= 68.0):
        if htf_bias != "BULLISH" or vol >= vol_sma20 * 1.15:
            return _build_trade_levels(
                direction="SHORT",
                entry=current_price or cl,
                raw_sl=highest_recent,
                atr=atr,
                setup_name="Stock Learner Liquidity Sweep Reversal",
                score=94.0,
                candle_time=c_time
            )

    # 2. BOOMING BULLS (ANISH SINGH THAKUR) 9/20/50 EMA TREND PULLBACK
    trend_bullish = (htf_bias == "BULLISH") and (ema20 > ema50) and (cl > ema20) and (cl > ema9)
    touched_bull_zone = (l <= ema20 * 1.002) or (prev_l <= ema20 * 1.002)
    strong_bull_candle = (cl > o) and (body_ratio >= 0.50) and (upper_wick_ratio <= 0.30) and (cl > prev_h)
    if trend_bullish and touched_bull_zone and strong_bull_candle and (45.0 <= rsi <= 66.0):
        pullback_low = min(l, prev_l)
        return _build_trade_levels(
            direction="LONG",
            entry=current_price or cl,
            raw_sl=pullback_low,
            atr=atr,
            setup_name="Booming Bulls EMA20 Trend Pullback",
            score=89.0,
            candle_time=c_time
        )

    trend_bearish = (htf_bias == "BEARISH") and (ema20 < ema50) and (cl < ema20) and (cl < ema9)
    touched_bear_zone = (h >= ema20 * 0.998) or (prev_h >= ema20 * 0.998)
    strong_bear_candle = (cl < o) and (body_ratio >= 0.50) and (lower_wick_ratio <= 0.30) and (cl < prev_l)
    if trend_bearish and touched_bear_zone and strong_bear_candle and (34.0 <= rsi <= 55.0):
        pullback_high = max(h, prev_h)
        return _build_trade_levels(
            direction="SHORT",
            entry=current_price or cl,
            raw_sl=pullback_high,
            atr=atr,
            setup_name="Booming Bulls EMA20 Trend Pullback",
            score=89.0,
            candle_time=c_time
        )

    return None
