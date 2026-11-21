import time, math, logging, requests
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import config

log = logging.getLogger("DataEngine")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 QuantTerminal/4.0"})

MARKET_STORE: Dict[str, Dict[str, Any]] = {}
_LAST_RADAR_TS = 0.0
_PREV_TICKER_PRICES: Dict[str, float] = {}
_DELTA_NATIVE_MAP: Dict[str, str] = {}
_BINANCE_LIQUID_CACHE = {"ts": 0.0, "syms": set()}

_CORE_ANCHORS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "PAXGUSDT", "XRPUSDT", "DOGEUSDT", "SUIUSDT", "LINKUSDT",
    "ADAUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "INJUSDT", "TONUSDT", "TIAUSDT", "SEIUSDT", "FETUSDT", "RENDERUSDT", "WIFUSDT",
    "PEPEUSDT", "SHIBUSDT", "BONKUSDT", "FLOKIUSDT", "ONDOUSDT", "ENAUSDT", "JUPUSDT", "STXUSDT",
    "FILUSDT", "ATOMUSDT", "HBARUSDT", "ICPUSDT", "UNIUSDT", "AAVEUSDT", "PENDLEUSDT", "BCHUSDT",
]

_BLOCKED_TICKERS = {
    "AMDB", "TQQQB", "TSLAB", "NVDAB", "AAPLB", "MSFTB", "GOOGB", "METAB", "AMZNB", "COINB",
    "SPY", "QQQ", "OIL", "USOIL", "UKOIL", "WTI", "BRENT", "NGAS", "NATGAS", "XAG", "SILVER",
    "RARE", "H", "SPX", "NDX", "DXY", "EUR", "GBP", "JPY",
}

def _get_binance_liquid_set() -> set:
    now = time.time()
    if _BINANCE_LIQUID_CACHE["syms"] and (now - _BINANCE_LIQUID_CACHE["ts"]) < 300.0:
        return _BINANCE_LIQUID_CACHE["syms"]
    min_b_vol = float(getattr(config, "MIN_BINANCE_VOL_USD", 25000000.0))
    for b_url in ("https://data-api.binance.vision/api/v3/ticker/24hr", "https://api.binance.com/api/v3/ticker/24hr"):
        try:
            r = HTTP.get(b_url, timeout=5.0)
            if r.status_code == 200:
                valid = {
                    str(it["symbol"]).upper()
                    for it in r.json()
                    if str(it.get("symbol", "")).upper().endswith("USDT")
                    and float(it.get("quoteVolume") or 0.0) >= min_b_vol
                }
                if len(valid) >= 20:
                    valid.add("PAXGUSDT")
                    _BINANCE_LIQUID_CACHE.update(ts=now, syms=valid)
                    return valid
        except Exception:
            continue
    return set(_CORE_ANCHORS)

def get_market(symbol: str = "BTCUSDT") -> Dict[str, Any]:
    sym = symbol.strip().upper()
    if sym not in MARKET_STORE:
        MARKET_STORE[sym] = {
            "symbol": sym, "price": None, "price_ts": 0, "klines": {}, "klines_ts": 0,
            "session": "INITIALIZING", "in_killzone": False, "killzone_name": "OFF_HOURS",
            "is_dead_hour": False, "cvd_ratio": 0.50, "poc": None, "funding_rate": 0.0001,
            "pdh": None, "pdl": None, "daily_open": None, "day_high_24h": None, "day_low_24h": None,
            "asian_high": None, "asian_low": None, "news_blackout": False, "error": None,
        }
    return MARKET_STORE[sym]

MARKET = get_market("BTCUSDT")

def session_and_killzone() -> tuple:
    now_utc = datetime.now(timezone.utc)
    h, m = now_utc.hour, now_utc.minute
    total_m = h * 60 + m
    session_name, in_kz, kz_name, is_dead_hour = "SESSION CLOSE", False, "OFF_HOURS", False
    if total_m >= 1170:
        is_dead_hour, session_name = True, "DEAD ZONE (OFF-PEAK)"
    elif h < 7:
        session_name = "ASIAN RANGE"
    elif h < 13:
        session_name = "LONDON EXPANSION"
    elif h < 21:
        session_name = "NEW YORK TREND"
    if 420 <= total_m <= 660:
        in_kz, kz_name = True, "LONDON KILLZONE"
    elif 750 <= total_m <= 1050:
        in_kz, kz_name = True, "NEW YORK KILLZONE"
    return session_name, in_kz, kz_name, is_dead_hour

def check_economic_news_blackout() -> bool:
    now_utc = datetime.now(timezone.utc)
    minute_of_day = now_utc.hour * 60 + now_utc.minute
    if now_utc.weekday() in [2, 3]:
        if abs(minute_of_day - 750) <= 25 or abs(minute_of_day - 1080) <= 25:
            return True
    return False

def calc_volume_delta(candles_15m: list) -> float:
    if len(candles_15m) < 3:
        return 0.50
    total_b, total_s = 0.0, 0.0
    for c in candles_15m[-10:]:
        h, l, cl, v = float(c.get("high", 0.0)), float(c.get("low", 0.0)), float(c.get("close", 0.0)), float(c.get("vol", 1.0))
        rng = max(1e-6, h - l)
        total_b += v * ((cl - l) / rng)
        total_s += v * ((h - cl) / rng)
    return round(total_b / (total_b + total_s + 1e-9), 3)

def fetch_funding_rate(symbol: str) -> float:
    params = config.get_asset_params(symbol, get_market(symbol).get("price"))
    delta_sym = _DELTA_NATIVE_MAP.get(symbol, params["delta_symbol"])
    try:
        r = HTTP.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={params['bin_symbol']}", timeout=3.5)
        if r.status_code == 200:
            return float(r.json().get("lastFundingRate", 0.0001))
    except Exception:
        pass
    for d_base in ("https://api.india.delta.exchange", "https://api.delta.exchange"):
        try:
            r = HTTP.get(f"{d_base}/v2/tickers/{delta_sym}", timeout=3.5)
            if r.status_code == 200:
                return float(r.json().get("result", {}).get("funding_rate", 0.0001))
        except Exception:
            pass
    return 0.0001

def fetch_price(symbol: str) -> Optional[float]:
    params = config.get_asset_params(symbol, get_market(symbol).get("price"))
    delta_sym = _DELTA_NATIVE_MAP.get(symbol, params["delta_symbol"])
    for b_base in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            r = HTTP.get(f"{b_base}/api/v3/ticker/price", params={"symbol": params["bin_symbol"]}, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200:
                p = float(r.json()["price"])
                if math.isfinite(p) and p > 0:
                    return p
        except Exception:
            pass
    for d_base in ("https://api.india.delta.exchange", "https://api.delta.exchange"):
        try:
            r = HTTP.get(f"{d_base}/v2/tickers/{delta_sym}", timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200:
                p = float(r.json().get("result", {}).get("mark_price", 0))
                if math.isfinite(p) and p > 0:
                    return p
        except Exception:
            pass
    try:
        r = HTTP.get(f"https://api.coinbase.com/v2/prices/{params['cb_symbol']}/spot", timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["data"]["amount"])
            if math.isfinite(p) and p > 0:
                return p
    except Exception:
        pass
    return None

def fetch_klines(symbol: str, interval: str, limit: int = 120) -> List[Dict[str, float]]:
    gran = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(interval, 900)
    m_dict = get_market(symbol)
    params = config.get_asset_params(symbol, m_dict.get("price"))
    delta_sym = _DELTA_NATIVE_MAP.get(symbol, params["delta_symbol"])

    for b_base in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            r = HTTP.get(f"{b_base}/api/v3/klines?symbol={params['bin_symbol']}&interval={interval}&limit={limit}", timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200:
                raw = r.json()
                if isinstance(raw, list) and len(raw) >= 15:
                    out = [{"time": int(k[0]) // 1000, "close_time": (int(k[0]) // 1000) + gran, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "vol": float(k[5])} for k in raw]
                    out.sort(key=lambda z: z["time"])
                    return out[-limit:]
        except Exception:
            pass

    for d_base in ("https://api.india.delta.exchange", "https://api.delta.exchange"):
        try:
            end_ts = int(time.time())
            start_ts = end_ts - (limit * gran)
            r = HTTP.get(f"{d_base}/v2/history/candles?resolution={interval}&symbol={delta_sym}&start={start_ts}&end={end_ts}", timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200:
                raw = r.json().get("result", [])
                if isinstance(raw, list) and len(raw) >= 15:
                    out = [{"time": int(k["time"]), "close_time": int(k["time"]) + gran, "open": float(k["open"]), "high": float(k["high"]), "low": float(k["low"]), "close": float(k["close"]), "vol": float(k.get("volume", 1.0))} for k in raw]
                    out.sort(key=lambda z: z["time"])
                    return out[-limit:]
        except Exception:
            pass

    try:
        r = HTTP.get(f"https://api.exchange.coinbase.com/products/{params['cb_symbol']}/candles?granularity={gran}", timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            raw = r.json()
            if isinstance(raw, list) and len(raw) >= 15:
                out = [{"time": int(k[0]), "close_time": int(k[0]) + gran, "open": float(k[3]), "high": float(k[2]), "low": float(k[1]), "close": float(k[4]), "vol": float(k[5])} for k in raw]
                out.sort(key=lambda z: z["time"])
                return out[-limit:]
    except Exception:
        pass

    return []

def extract_pdh_pdl(daily_candles: list) -> tuple:
    if len(daily_candles) >= 2:
        prev_day = daily_candles[-2]
        return float(prev_day["high"]), float(prev_day["low"]), float(daily_candles[-1].get("open", prev_day["close"]))
    elif len(daily_candles) == 1:
        return float(daily_candles[0]["high"]), float(daily_candles[0]["low"]), float(daily_candles[0]["open"])
    return None, None, None

def extract_24h_levels(candles_15m: list) -> tuple:
    bars_24h = candles_15m[-96:] if len(candles_15m) >= 96 else candles_15m
    if bars_24h:
        return max(c["high"] for c in bars_24h), min(c["low"] for c in bars_24h), sum(c.get("vol", 0.0) for c in bars_24h), float(bars_24h[0]["open"])
    return None, None, 0.0, None

def extract_asian_range(candles_15m: list) -> tuple:
    now_dt = datetime.now(timezone.utc)
    t0 = int(datetime(now_dt.year, now_dt.month, now_dt.day, 0, 0, tzinfo=timezone.utc).timestamp())
    asian_bars = [c for c in candles_15m if t0 <= c.get("time", 0) <= t0 + 25200]
    if asian_bars:
        return max(c["high"] for c in asian_bars), min(c["low"] for c in asian_bars)
    return None, None

def approx_poc(cs: List[Dict[str, float]], bins: int = 30) -> Optional[float]:
    if not cs:
        return None
    lo, hi = min(c["low"] for c in cs), max(c["high"] for c in cs)
    if hi <= lo:
        return lo
    width = (hi - lo) / bins
    vol = [0.0] * bins
    for c in cs:
        idx = min(bins - 1, max(0, int((((c["high"] + c["low"] + c["close"]) / 3) - lo) / width)))
        vol[idx] += c["vol"]
    return lo + (max(range(bins), key=lambda n: vol[n]) + 0.5) * width

def refresh_symbol_market_data(symbol: str, lock) -> bool:
    try:
        p = fetch_price(symbol)
        if not p or p <= 0:
            return False
        sess, in_kz, kz_name, is_dead = session_and_killzone()
        news_block = check_economic_news_blackout()
        funding = fetch_funding_rate(symbol)
        with lock:
            m = get_market(symbol)
            m.update(price=p, price_ts=time.time(), session=sess, in_killzone=in_kz, killzone_name=kz_name, is_dead_hour=is_dead, news_blackout=news_block, funding_rate=funding)
        ks = {}
        for k in ("15m", "1h", "4h", "1d"):
            bars = fetch_klines(symbol, k, 120)
            if not bars and k == "15m":
                return False
            ks[k] = bars
            time.sleep(0.01)
        pdh, pdl, d_open = extract_pdh_pdl(ks.get("1d", []))
        h24, l24, _, open24 = extract_24h_levels(ks.get("15m", []))
        arh, arl = extract_asian_range(ks.get("15m", []))
        poc = approx_poc(ks.get("15m", [])[-96:])
        cvd = calc_volume_delta(ks.get("15m", []))
        with lock:
            get_market(symbol).update(klines=ks, klines_ts=time.time(), poc=poc, pdh=pdh, pdl=pdl, daily_open=d_open or open24, day_high_24h=h24, day_low_24h=l24, asian_high=arh, asian_low=arl, cvd_ratio=cvd, error=None)
        return True
    except Exception as e:
        with lock:
            get_market(symbol)["error"] = str(e)[:250]
        return False

def discover_top_delta_movers(active_trade_symbols: list = None) -> List[str]:
    global _LAST_RADAR_TS
    now = time.time()
    if not getattr(config, "ENABLE_ALL_DELTA_RADAR", True):
        return config.SYMBOLS
    if (now - _LAST_RADAR_TS) < 30.0 and len(config.SYMBOLS) >= 30:
        for s in (active_trade_symbols or []):
            if s and s not in config.SYMBOLS:
                config.SYMBOLS.insert(0, s)
        return config.SYMBOLS

    binance_liquid = _get_binance_liquid_set()
    selected = []
    for s in (active_trade_symbols or []) + _CORE_ANCHORS:
        if s and s not in selected:
            selected.append(s)

    instant_spikers, movers = [], []
    min_turn = float(getattr(config, "MIN_RADAR_TURNOVER_USD", 1500000.0))

    for d_url in ("https://api.india.delta.exchange/v2/tickers", "https://api.delta.exchange/v2/tickers"):
        try:
            r = HTTP.get(d_url, timeout=4.5)
            if r.status_code == 200:
                items = r.json().get("result", [])
                if items:
                    for it in items:
                        ctype = str(it.get("contract_type", "")).lower()
                        sym_raw = str(it.get("symbol", "")).upper()
                        if (ctype and "perpetual" not in ctype) or (not sym_raw.endswith("USD") and not sym_raw.endswith("USDT")):
                            continue
                        base = sym_raw[:-4] if sym_raw.endswith("USDT") else sym_raw[:-3]
                        if not base.isalnum() or len(base) < 2 or len(base) > 10 or base in ("USDT", "USDC", "DAI"):
                            continue
                        if base in _BLOCKED_TICKERS or (len(base) >= 4 and base.endswith("B") and base not in ("SHIB", "BNB", "ARB", "KAVA", "HBAR")):
                            continue
                        std_sym = f"{base}USDT"
                        if std_sym not in binance_liquid and std_sym not in _CORE_ANCHORS:
                            continue
                        _DELTA_NATIVE_MAP[std_sym] = sym_raw
                        mark_p = float(it.get("mark_price") or it.get("close") or 0.0)
                        open_p = float(it.get("open") or mark_p)
                        vol_raw = float(it.get("volume") or 0.0)
                        turnover = max(float(it.get("turnover_usd") or 0.0), float(it.get("turnover") or 0.0), vol_raw * mark_p)
                        if mark_p <= 0 or open_p <= 0 or turnover < min_turn:
                            continue
                        prev_p = _PREV_TICKER_PRICES.get(std_sym)
                        _PREV_TICKER_PRICES[std_sym] = mark_p
                        jump_pct = abs((mark_p - prev_p) / prev_p) * 100.0 if (prev_p and prev_p > 0) else 0.0
                        m_entry = get_market(std_sym)
                        m_entry["price"] = mark_p
                        m_entry["price_ts"] = now
                        day_move = abs((mark_p - open_p) / open_p) * 100.0
                        if jump_pct >= 0.35:
                            instant_spikers.append((jump_pct, day_move, std_sym))
                        elif std_sym not in selected:
                            movers.append((day_move, turnover, std_sym))
                    break
        except Exception:
            continue

    instant_spikers.sort(key=lambda x: x[0], reverse=True)
    for _, _, sp_sym in instant_spikers:
        if sp_sym not in selected:
            selected.insert(0, sp_sym)
    movers.sort(key=lambda x: (x[0], x[1]), reverse=True)
    max_tot = int(getattr(config, "MAX_RADAR_SYMBOLS", 45))
    for _, _, m_sym in movers:
        if len(selected) >= max_tot:
            break
        if m_sym not in selected:
            selected.append(m_sym)
    config.SYMBOLS = selected[:max_tot]
    _LAST_RADAR_TS = now
    return config.SYMBOLS

def refresh_all_market_data(lock, active_trade_symbols: list = None) -> bool:
    discover_top_delta_movers(active_trade_symbols)
    success = False
    with ThreadPoolExecutor(max_workers=min(24, max(4, len(config.SYMBOLS)))) as pool:
        futures = {pool.submit(refresh_symbol_market_data, sym, lock): sym for sym in list(config.SYMBOLS)}
        for fut in as_completed(futures):
            try:
                if fut.result():
                    success = True
            except Exception:
                pass
    return success

def refresh_market_data(lock) -> bool:
    return refresh_all_market_data(lock)
