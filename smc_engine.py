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
    """
    Computes:
    - 1-Day (24-Hour / 96-bar) Swing Extremes & Daily Liquidity Pools (PDH, PDL, 24h High/Low)
    - Macro Multi-Timeframe Trend Consensus (1H EMA50/200 + Daily Open + 15M EMA200)
    - Anti-Whipsaw Regime Classifier (ADX + Bollinger Bandwidth)
    """
    klines = market_dict.get("klines", {})
    k15 = klines.get("15m", [])
    k1h = klines.get("1h", [])
    if len(k15) < 35:
        return {}

    p15 = [_parse_candle(c) for c in k15]
    closed_15 = p15[:-1] # Confirmed closed candles only
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

    # Bollinger Bandwidth for Chop Detection
    bb_mid = sum(closes[-20:]) / 20.0
    bb_variance = sum((x - bb_mid) ** 2 for x in closes[-20:]) / 20.0
    bb_std = bb_variance ** 0.5
    bb_bandwidth = (4.0 * bb_std) / (bb_mid + 1e-9)

    # 1-Day (24-Hour) Swing lookback (96 bars of 15m)
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

    # Multi-Timeframe Macro Trend Consensus (1H + 15M + Daily Open)
    bull_score = 0
    bear_score = 0

    if cur_close > ema200:
        bull_score += 1
    else:
        bear_score += 1

    closes_1h = []
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

    if daily_open and daily_open > 0:
        if cur_close > daily_open:
            bull_score += 2
        else:
            bear_score += 2

    if bull_score >= 4:
        macro_bias = "BULLISH"
    elif bear_score >= 4:
        macro_bias = "BEARISH"
    else:
        macro_bias = "NEUTRAL"

    # Market Regime Classification (Anti-Chop)
    # A market is choppy ONLY when ADX < 20 (no trend) AND bandwidth or EMAs are flat.
    ema_spread_pct = abs(ema20 - ema50) / ema50
    is_choppy = (adx_15m < 20.0) and ((bb_bandwidth < 0.004) or (ema_spread_pct < 0.0015))

    funding_rate = market_dict.get("funding_rate", 0.0001)
    in_killzone = market_dict.get("in_killzone", False)
    kz_name = market_dict.get("killzone_name", "OFF_HOURS")
    news_blackout = market_dict.get("news_blackout", False)

    return {
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
        "macro_bias": macro_bias,
        "htf_bias": macro_bias,
        "is_choppy": is_choppy,
        "funding_rate": funding_rate,
        "in_killzone": in_killzone,
        "killzone_name": kz_name,
        "news_blackout": news_blackout
    }

def _build_trade_levels(direction: str, entry: float, raw_sl: float, atr: float, setup_name: str, score: float, candle_time: int) -> dict:
    """
    Enforces volatility-normalized wide Stop-Loss distance (380 - 620 pts on BTC)
    so natural market noise and liquidity wicks cannot hunt the stop.
    Targets: TP1 = 1.4R (Book 50% & Trail to BE + buffer), TP2 = 2.4R, TP3 = 3.5R Runner.
    """
    sl_buffer = getattr(config, "SL_BUFFER", 50.0)
    min_stop = getattr(config, "MIN_STOP", 380.0)
    max_stop = getattr(config, "MAX_STOP", 620.0)

    if direction == "LONG":
        structural_dist = (entry - raw_sl) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.7)))
        sl = round(entry - sl_dist, 2)
        tp1 = round(entry + (sl_dist * 1.4), 2)
        tp2 = round(entry + (sl_dist * 2.4), 2)
        tp3 = round(entry + (sl_dist * 3.5), 2)
    else:
        structural_dist = (raw_sl - entry) + sl_buffer
        sl_dist = max(min_stop, min(max_stop, max(structural_dist, atr * 1.7)))
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
    """
    Institutional 1-Day Confluence Execution Engine:
    1. STRICT TREND LOCK: Zero counter-trend shorts when 1D/1H is Bullish; zero counter-trend longs when Bearish.
    2. INSTITUTIONAL DAILY BREAKOUT: If price blasts above PDH with strong body and volume in a Bull Trend, ride the LONG expansion!
    3. 1-DAY (24-HOUR) LIQUIDITY SWEEP: True rejection wicks (>= 38%) at verified 24h/PDH/PDL levels.
    4. 24-HOUR TREND CONTINUATION PULLBACK: Active only when ADX >= 22 in non-choppy regime.
    """
    if len(candles_15m) < 35 or not structure:
        return None

    if structure.get("news_blackout", False):
        log.info("Signal skipped: Economic news blackout active.")
        return None

    strict_kz = getattr(config, "STRICT_KILLZONE_ONLY", False)
    if strict_kz and not structure.get("in_killzone", False):
        return None

    funding = structure.get("funding_rate", 0.0001)
    max_long_funding = getattr(config, "MAX_FUNDING_FOR_LONG", 0.00035)
    min_short_funding = getattr(config, "MIN_FUNDING_FOR_SHORT", -0.00020)

    is_choppy = structure.get("is_choppy", False)
    adx = structure.get("adx", 20.0)
    macro_bias = structure.get("macro_bias", "NEUTRAL")
    strict_trend_lock = getattr(config, "STRICT_TREND_LOCK", True)

    parsed = [_parse_candle(c) for c in candles_15m]
    c = parsed[-2] # Last confirmed closed candle
    prev = parsed[-3]
    o, h, l, cl, vol, c_time = c[0], c[1], c[2], c[3], c[4], c[5]
    prev_o, prev_h, prev_l, prev_cl = prev[0], prev[1], prev[2], prev[3]

    atr = structure.get("atr", 220.0)
    sh = structure["swing_high"] # 24-hour High
    sl = structure["swing_low"]  # 24-hour Low
    pdh = structure.get("pdh")
    pdl = structure.get("pdl")
    day_high_24h = structure.get("day_high_24h")
    day_low_24h = structure.get("day_low_24h")
    asian_high = structure.get("asian_high")
    asian_low = structure.get("asian_low")

    ema9 = structure["ema9"]
    ema20 = structure["ema20"]
    ema50 = structure["ema50"]
    ema200 = structure["ema200"]
    rsi = structure["rsi"]
    vol_sma20 = structure.get("vol_sma20", 1.0)

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

    # ==========================================================================
    # STRATEGY 1: INSTITUTIONAL DAILY EXPANSION BREAKOUT (RIDE THE MOMENTUM)
    # When price punches through PDH in a Bull Trend, NEVER short it! Ride the LONG!
    # ==========================================================================
    enable_breakout = getattr(config, "ENABLE_DAILY_BREAKOUT", True)
    if enable_breakout and not is_choppy:
        # Bullish Daily Breakout above PDH or 24h High
        breakout_high = pdh or day_high_24h
        if breakout_high and macro_bias == "BULLISH" and funding <= max_long_funding:
            broke_above = (highest_recent >= breakout_high + 10.0) and (cl > breakout_high)
            strong_expansion_bar = (cl > o) and (body_ratio >= 0.45) and (upper_wick_ratio <= 0.30)
            vol_confirmed = vol >= vol_sma20 * 1.02
            rsi_aligned = rsi >= 48.0
            
            if broke_above and strong_expansion_bar and vol_confirmed and rsi_aligned:
                breakout_anchor = min(l, prev_l, ema20)
                return _build_trade_levels(
                    direction="LONG",
                    entry=current_price or cl,
                    raw_sl=breakout_anchor,
                    atr=atr,
                    setup_name="Institutional Daily High Expansion Breakout",
                    score=98.0,
                    candle_time=c_time
                )

        # Bearish Daily Breakdown below PDL or 24h Low
        breakdown_low = pdl or day_low_24h
        if breakdown_low and macro_bias == "BEARISH" and funding >= min_short_funding:
            broke_below = (lowest_recent <= breakdown_low - 10.0) and (cl < breakdown_low)
            strong_dump_bar = (cl < o) and (body_ratio >= 0.45) and (lower_wick_ratio <= 0.30)
            vol_confirmed = vol >= vol_sma20 * 1.02
            rsi_aligned = rsi <= 52.0
            
            if broke_below and strong_dump_bar and vol_confirmed and rsi_aligned:
                breakdown_anchor = max(h, prev_h, ema20)
                return _build_trade_levels(
                    direction="SHORT",
                    entry=current_price or cl,
                    raw_sl=breakdown_anchor,
                    atr=atr,
                    setup_name="Institutional Daily Low Expansion Breakdown",
                    score=98.0,
                    candle_time=c_time
                )

    # ==========================================================================
    # STRATEGY 2: INSTITUTIONAL 1-DAY (PDH/PDL / 24H) LIQUIDITY TRAP REVERSAL
    # STRICT: Never take counter-trend sweep against Macro Bullish or Bearish Bias!
    # ==========================================================================
    daily_low_target = pdl or day_low_24h or asian_low
    # Long sweep only allowed if Macro Bias is NOT Bearish
    allow_long_sweep = (macro_bias != "BEARISH") if strict_trend_lock else True
    if daily_low_target and allow_long_sweep and funding <= max_long_funding:
        swept_daily_low = (lowest_recent <= daily_low_target - 15.0) and (cl > daily_low_target)
        bullish_rejection = (cl > o) and (lower_wick_ratio >= 0.38 or body_ratio >= 0.45) and (rsi <= 48.0)
        if swept_daily_low and bullish_rejection:
            return _build_trade_levels(
                direction="LONG",
                entry=current_price or cl,
                raw_sl=lowest_recent,
                atr=atr,
                setup_name="Institutional 1-Day Low Liquidity Trap",
                score=96.0,
                candle_time=c_time
            )

    daily_high_target = pdh or day_high_24h or asian_high
    # Short sweep allowed ONLY if Macro Bias is NOT Bullish! (Prevents suicidal shorts during bull rallies!)
    allow_short_sweep = (macro_bias != "BULLISH") if strict_trend_lock else True
    if daily_high_target and allow_short_sweep and funding >= min_short_funding:
        swept_daily_high = (highest_recent >= daily_high_target + 15.0) and (cl < daily_high_target)
        bearish_rejection = (cl < o) and (upper_wick_ratio >= 0.38 or body_ratio >= 0.45) and (rsi >= 52.0)
        if swept_daily_high and bearish_rejection:
            return _build_trade_levels(
                direction="SHORT",
                entry=current_price or cl,
                raw_sl=highest_recent,
                atr=atr,
                setup_name="Institutional 1-Day High Liquidity Trap",
                score=96.0,
                candle_time=c_time
            )

    # ==========================================================================
    # STRATEGY 3: 24-HOUR MACRO SWING LIQUIDITY TRAP (96-BAR LOOKBACK)
    # ==========================================================================
    if allow_long_sweep and funding <= max_long_funding:
        swept_low = (lowest_recent <= sl - 15.0) and (cl > sl)
        bullish_reclaim = (cl > o) and (lower_wick_ratio >= 0.38 or body_ratio >= 0.45) and (cl > prev_h or cl > ema9)
        if swept_low and bullish_reclaim and (rsi <= 48.0) and (vol >= vol_sma20 * 1.05):
            return _build_trade_levels(
                direction="LONG",
                entry=current_price or cl,
                raw_sl=lowest_recent,
                atr=atr,
                setup_name="24-Hour Macro Swing Liquidity Trap",
                score=93.0,
                candle_time=c_time
            )

    if allow_short_sweep and funding >= min_short_funding:
        swept_high = (highest_recent >= sh + 15.0) and (cl < sh)
        bearish_reclaim = (cl < o) and (upper_wick_ratio >= 0.38 or body_ratio >= 0.45) and (cl < prev_l or cl < ema9)
        if swept_high and bearish_reclaim and (rsi >= 52.0) and (vol >= vol_sma20 * 1.05):
            return _build_trade_levels(
                direction="SHORT",
                entry=current_price or cl,
                raw_sl=highest_recent,
                atr=atr,
                setup_name="24-Hour Macro Swing Liquidity Trap",
                score=93.0,
                candle_time=c_time
            )

    # ==========================================================================
    # STRATEGY 4: TRAFFIC-LIGHT TREND CONTINUATION PULLBACK
    # CRITICAL: 100% DISABLED DURING SIDEWAYS CHOP OR WHEN ADX < 22!
    # ==========================================================================
    min_adx = getattr(config, "MIN_ADX_TREND", 22.0)
    if not is_choppy and adx >= min_adx:
        # Bullish Pullback: Macro Bullish + Price > 9 > 20 > 50 > 200 EMA
        trend_bullish = (macro_bias == "BULLISH") and (cl > ema200) and (ema9 > ema20 > ema50 > ema200)
        touched_bull_zone = (l <= ema20 * 1.0015) and (cl >= ema20 * 0.999)
        strong_bull_candle = (cl > o) and (body_ratio >= 0.50) and (upper_wick_ratio <= 0.25) and (cl > prev_h)
        if trend_bullish and touched_bull_zone and strong_bull_candle and (48.0 <= rsi <= 68.0) and (funding <= max_long_funding):
            pullback_low = min(l, prev_l)
            return _build_trade_levels(
                direction="LONG",
                entry=current_price or cl,
                raw_sl=pullback_low,
                atr=atr,
                setup_name="Traffic-Light EMA Trend Pullback",
                score=91.0,
                candle_time=c_time
            )

        # Bearish Pullback: Macro Bearish + Price < 9 < 20 < 50 < 200 EMA
        trend_bearish = (macro_bias == "BEARISH") and (cl < ema200) and (ema9 < ema20 < ema50 < ema200)
        touched_bear_zone = (h >= ema20 * 0.9985) and (cl <= ema20 * 1.001)
        strong_bear_candle = (cl < o) and (body_ratio >= 0.50) and (lower_wick_ratio <= 0.25) and (cl < prev_l)
        if trend_bearish and touched_bear_zone and strong_bear_candle and (32.0 <= rsi <= 52.0) and (funding >= min_short_funding):
            pullback_high = max(h, prev_h)
            return _build_trade_levels(
                direction="SHORT",
                entry=current_price or cl,
                raw_sl=pullback_high,
                atr=atr,
                setup_name="Traffic-Light EMA Trend Pullback",
                score=91.0,
                candle_time=c_time
            )

    return None
