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
        diff = abs(di_plus - di_minus)
        total = di_plus + di_minus + 1e-9
        dx.append((diff / total) * 100.0)
    if len(dx) < period:
        return sum(dx) / max(1, len(dx))
    adx = sum(dx[:period]) / period
    for d in dx[period:]:
        adx = (adx * (period - 1) + d) / period
    return round(float(adx), 1)

def get_structure(market_dict: dict) -> dict:
    klines = market_dict.get("klines", {})
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
    adx_15m = _calc_adx(highs, lows, closes, 14)
    vol_sma20 = sum(vols[-20:]) / min(len(vols), 20)

    ema9 = _calc_ema(closes, 9)
    ema20 = _calc_ema(closes, 20)
    ema50 = _calc_ema(closes, 50)
    ema200 = _calc_ema(closes, min(len(closes), 200))

    htf_bias = "NEUTRAL"
    if len(k1h) >= 20:
        p1h = [_parse_candle(c) for c in k1h[:-1]]
        closes_1h = [p[3] for p in p1h]
        ema50_1h = _calc_ema(closes_1h, 50)
        ema200_1h = _calc_ema(closes_1h, min(len(closes_1h), 200))
        if closes_1h[-1] > ema50_1h and ema50_1h >= ema200_1h * 0.995:
            htf_bias = "BULLISH"
        elif closes_1h[-1] < ema50_1h and ema50_1h <= ema200_1h * 1.005:
            htf_bias = "BEARISH"
    else:
        htf_bias = "BULLISH" if cur_close > ema200 else "BEARISH"

    swing_lookback = min(len(closed_15) - 4, 35)
    swing_high = max(highs[-swing_lookback - 3 : -3])
    swing_low = min(lows[-swing_lookback - 3 : -3])

    ema_spread_pct = abs(ema20 - ema50) / ema50
    is_choppy = ema_spread_pct < 0.0018

    pdh = market_dict.get("pdh")
    pdl = market_dict.get("pdl")
    asian_high = market_dict.get("asian_high")
    asian_low = market_dict.get("asian_low")
    funding_rate = market_dict.get("funding_rate", 0.0001)
    in_killzone = market_dict.get("in_killzone", False)
    kz_name = market_dict.get("killzone_name", "OFF_HOURS")
    news_blackout = market_dict.get("news_blackout", False)

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "pdh": pdh,
        "pdl": pdl,
        "asian_high": asian_high,
        "asian_low": asian_low,
        "current_close": cur_close,
        "ema9": ema9,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi_15m,
        "adx": adx_15m,
        "vol_sma20": vol_sma20,
        "atr": atr_15m,
        "htf_bias": htf_bias,
        "is_choppy": is_choppy,
        "funding_rate": funding_rate,
        "in_killzone": in_killzone,
        "killzone_name": kz_name,
        "news_blackout": news_blackout
    }

def _build_trade_levels(direction: str, entry: float, raw_sl: float, atr: float, setup_name: str, score: float, candle_time: int) -> dict:
    sl_buffer = getattr(config, "SL_BUFFER", 45.0)
    min_stop = getattr(config, "MIN_STOP", 320.0)
    max_stop = getattr(config, "MAX_STOP", 580.0)

    if direction == "LONG":
        structural_dist = (entry - raw_sl) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.6)))
        sl = round(entry - sl_dist, 2)
        tp1 = round(entry + (sl_dist * 1.4), 2)
        tp2 = round(entry + (sl_dist * 2.4), 2)
        tp3 = round(entry + (sl_dist * 3.5), 2)
    else:
        structural_dist = (raw_sl - entry) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.6)))
        sl = round(entry + sl_dist, 2)
        tp1 = round(entry - (sl_dist * 1.4), 2)
        tp2 = round(entry - (sl_dist * 2.4), 2)
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

    if structure.get("news_blackout", False):
        log.info("Signal skipped: High-impact economic news blackout active.")
        return None

    strict_kz = getattr(config, "STRICT_KILLZONE_ONLY", True)
    if strict_kz and not structure.get("in_killzone", False):
        return None

    funding = structure.get("funding_rate", 0.0001)
    max_long_funding = getattr(config, "MAX_FUNDING_FOR_LONG", 0.00035)
    min_short_funding = getattr(config, "MIN_FUNDING_FOR_SHORT", -0.00020)

    is_choppy = structure.get("is_choppy", False)
    adx = structure.get("adx", 20.0)

    parsed = [_parse_candle(c) for c in candles_15m]
    c = parsed[-2]
    prev = parsed[-3]
    o, h, l, cl, vol, c_time = c[0], c[1], c[2], c[3], c[4], c[5]
    prev_o, prev_h, prev_l, prev_cl = prev[0], prev[1], prev[2], prev[3]

    atr = structure.get("atr", 220.0)
    sh = structure["swing_high"]
    sl = structure["swing_low"]
    pdh = structure.get("pdh")
    pdl = structure.get("pdl")
    asian_high = structure.get("asian_high")
    asian_low = structure.get("asian_low")

    ema9 = structure["ema9"]
    ema20 = structure["ema20"]
    ema50 = structure["ema50"]
    ema200 = structure["ema200"]
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

    recent_closed = parsed[-6:-1]
    lowest_recent = min(x[2] for x in recent_closed)
    highest_recent = max(x[1] for x in recent_closed)

    # 1. GAUTAM JHA PREVIOUS DAY (PDH/PDL) OR ASIAN RANGE SWEEP (SCORE: 98.0)
    daily_low_target = pdl or asian_low
    if daily_low_target and funding <= max_long_funding:
        swept_daily_low = (lowest_recent <= daily_low_target - 10.0) and (cl > daily_low_target)
        if swept_daily_low and (cl > o) and (lower_wick_ratio >= 0.35 or body_ratio >= 0.45) and (rsi <= 45.0):
            return _build_trade_levels(
                direction="LONG",
                entry=current_price or cl,
                raw_sl=lowest_recent,
                atr=atr,
                setup_name="Gautam Jha PDH/PDL Daily Liquidity Trap",
                score=98.0,
                candle_time=c_time
            )

    daily_high_target = pdh or asian_high
    if daily_high_target and funding >= min_short_funding:
        swept_daily_high = (highest_recent >= daily_high_target + 10.0) and (cl < daily_high_target)
        if swept_daily_high and (cl < o) and (upper_wick_ratio >= 0.35 or body_ratio >= 0.45) and (rsi >= 55.0):
            return _build_trade_levels(
                direction="SHORT",
                entry=current_price or cl,
                raw_sl=highest_recent,
                atr=atr,
                setup_name="Gautam Jha PDH/PDL Daily Liquidity Trap",
                score=98.0,
                candle_time=c_time
            )

    # 2. STOCK LEARNER 35-BAR INTRADAY SWEEP
    if funding <= max_long_funding:
        swept_low = (lowest_recent <= sl - 10.0) and (cl > sl)
        bullish_reclaim = (cl > o) and (lower_wick_ratio >= 0.35 or body_ratio >= 0.45) and (cl > prev_h or cl > ema9)
        if swept_low and bullish_reclaim and (rsi <= 45.0) and (vol >= vol_sma20 * 1.05):
            return _build_trade_levels(
                direction="LONG",
                entry=current_price or cl,
                raw_sl=lowest_recent,
                atr=atr,
                setup_name="Stock Learner Liquidity Sweep Reversal",
                score=94.0,
                candle_time=c_time
            )

    if funding >= min_short_funding:
        swept_high = (highest_recent >= sh + 10.0) and (cl < sh)
        bearish_reclaim = (cl < o) and (upper_wick_ratio >= 0.35 or body_ratio >= 0.45) and (cl < prev_l or cl < ema9)
        if swept_high and bearish_reclaim and (rsi >= 55.0) and (vol >= vol_sma20 * 1.05):
            return _build_trade_levels(
                direction="SHORT",
                entry=current_price or cl,
                raw_sl=highest_recent,
                atr=atr,
                setup_name="Stock Learner Liquidity Sweep Reversal",
                score=94.0,
                candle_time=c_time
            )

    # 3. BOOMING BULLS 9/20/50/200 EMA PULLBACK (STRICT TREND ONLY)
    min_adx = getattr(config, "MIN_ADX_TREND", 21.0)
    if not is_choppy and adx >= min_adx:
        trend_bullish = (htf_bias == "BULLISH") and (cl > ema200) and (ema9 > ema20 > ema50 > ema200)
        touched_bull_zone = (l <= ema20 * 1.0015) and (cl >= ema20 * 0.999)
        strong_bull_candle = (cl > o) and (body_ratio >= 0.50) and (upper_wick_ratio <= 0.25) and (cl > prev_h)
        if trend_bullish and touched_bull_zone and strong_bull_candle and (48.0 <= rsi <= 65.0) and (funding <= max_long_funding):
            pullback_low = min(l, prev_l)
            return _build_trade_levels(
                direction="LONG",
                entry=current_price or cl,
                raw_sl=pullback_low,
                atr=atr,
                setup_name="Booming Bulls EMA20 Trend Pullback",
                score=90.0,
                candle_time=c_time
            )

        trend_bearish = (htf_bias == "BEARISH") and (cl < ema200) and (ema9 < ema20 < ema50 < ema200)
        touched_bear_zone = (h >= ema20 * 0.9985) and (cl <= ema20 * 1.001)
        strong_bear_candle = (cl < o) and (body_ratio >= 0.50) and (lower_wick_ratio <= 0.25) and (cl < prev_l)
        if trend_bearish and touched_bear_zone and strong_bear_candle and (35.0 <= rsi <= 52.0) and (funding >= min_short_funding):
            pullback_high = max(h, prev_h)
            return _build_trade_levels(
                direction="SHORT",
                entry=current_price or cl,
                raw_sl=pullback_high,
                atr=atr,
                setup_name="Booming Bulls EMA20 Trend Pullback",
                score=90.0,
                candle_time=c_time
            )

    return None
