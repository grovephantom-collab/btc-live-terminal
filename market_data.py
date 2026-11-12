import time, math, logging, requests
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import config

log = logging.getLogger("DataEngine")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 QuantTerminal/4.0"})

MARKET_STORE: Dict[str, Dict[str, Any]] = {}

def get_market(symbol: str = "BTCUSDT") -> Dict[str, Any]:
    sym = symbol.strip().upper()
    if sym not in MARKET_STORE:
        MARKET_STORE[sym] = {
            "symbol": sym, "price": None, "price_ts": 0, "klines": {}, "klines_ts": 0,
            "session": "INITIALIZING", "in_killzone": False, "killzone_name": "OFF_HOURS",
            "is_dead_hour": False, "cvd_ratio": 0.50,
            "poc": None, "funding_rate": 0.0001,
            "pdh": None, "pdl": None, "daily_open": None,
            "day_high_24h": None, "day_low_24h": None,
            "asian_high": None, "asian_low": None,
            "news_blackout": False, "error": None
        }
    return MARKET_STORE[sym]

MARKET = get_market("BTCUSDT")

def session_and_killzone() -> tuple:
    now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    m = now_utc.minute
    total_m = h * 60 + m

    session_name = "SESSION CLOSE"
    in_kz = False
    kz_name = "OFF_HOURS"
    is_dead_hour = False

    if total_m >= 1170:
        is_dead_hour = True
        session_name = "DEAD ZONE (OFF-PEAK)"
    elif h < 7:
        session_name = "ASIAN RANGE"
    elif h < 13:
        session_name = "LONDON EXPANSION"
    elif h < 21:
        session_name = "NEW YORK TREND"

    if 420 <= total_m <= 660:
        in_kz = True
        kz_name = "LONDON KILLZONE"
    elif 750 <= total_m <= 1050:
        in_kz = True
        kz_name = "NEW YORK KILLZONE"

    return session_name, in_kz, kz_name, is_dead_hour

def check_economic_news_blackout() -> bool:
    now_utc = datetime.now(timezone.utc)
    minute_of_day = now_utc.hour * 60 + now_utc.minute
    if now_utc.weekday() in [2, 3]:
        cpi_m = 12 * 60 + 30
        fomc_m = 18 * 60
        if abs(minute_of_day - cpi_m) <= 25 or abs(minute_of_day - fomc_m) <= 25:
            return True
    return False

def calc_volume_delta(candles_15m: list) -> float:
    if len(candles_15m) < 3:
        return 0.50
    buyer_vols, seller_vols = [], []
    for c in candles_15m[-10:]:
        h = float(c.get("high", 0.0))
        l = float(c.get("low", 0.0))
        cl = float(c.get("close", 0.0))
        v = float(c.get("vol", 1.0))
        rng = max(0.1, h - l)
        b_ratio = (cl - l) / rng
        s_ratio = (h - cl) / rng
        buyer_vols.append(v * b_ratio)
        seller_vols.append(v * s_ratio)
    total_b = sum(buyer_vols)
    total_s = sum(seller_vols)
    tot = total_b + total_s + 1e-9
    return round(total_b / tot, 3)

def fetch_funding_rate(symbol: str) -> float:
    params = config.get_asset_params(symbol)
    bin_sym = params["bin_symbol"]
    delta_sym = params["delta_symbol"]
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={bin_sym}"
        r = HTTP.get(url, timeout=3.5)
        if r.status_code == 200:
            return float(r.json().get("lastFundingRate", 0.0001))
    except Exception:
        pass
    try:
        url = f"https://api.delta.exchange/v2/tickers/{delta_sym}"
        r = HTTP.get(url, timeout=3.5)
        if r.status_code == 200:
            return float(r.json().get("result", {}).get("funding_rate", 0.0001))
    except Exception:
        pass
    return 0.0001

def fetch_price(symbol: str) -> Optional[float]:
    params = config.get_asset_params(symbol)
    cb_sym = params["cb_symbol"]
    bin_sym = params["bin_symbol"]
    delta_sym = params["delta_symbol"]

    try:
        r = HTTP.get(f"https://api.coinbase.com/v2/prices/{cb_sym}/spot", timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["data"]["amount"])
            if math.isfinite(p) and p > 0:
                return p
    except Exception:
        pass

    for b_base in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            r = HTTP.get(f"{b_base}/api/v3/ticker/price", params={"symbol": bin_sym}, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200:
                p = float(r.json()["price"])
                if math.isfinite(p) and p > 0:
                    return p
        except Exception:
            pass

    try:
        url = f"https://api.delta.exchange/v2/tickers/{delta_sym}"
        r = HTTP.get(url, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json().get("result", {}).get("mark_price", 0))
            if math.isfinite(p) and p > 0:
                return p
    except Exception:
        pass

    return None

def fetch_klines(symbol: str, interval: str, limit: int = 120) -> List[Dict[str, float]]:
    gran_map = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    gran = gran_map.get(interval, 900)
    params = config.get_asset_params(symbol)
    cb_sym = params["cb_symbol"]
    bin_sym = params["bin_symbol"]

    try:
        url = f"https://api.exchange.coinbase.com/products/{cb_sym}/candles?granularity={gran}"
        r = HTTP.get(url, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            raw = r.json()
            if isinstance(raw, list) and len(raw) >= 5:
                out = []
                for k in raw:
                    t = int(k[0])
                    out.append({
                        "time": t, "close_time": t + gran,
                        "open": float(k[3]), "high": float(k[2]),
                        "low": float(k[1]), "close": float(k[4]),
                        "vol": float(k[5])
                    })
                out.sort(key=lambda z: z["time"])
                return out[-limit:]
    except Exception:
        pass

    binance_intervals = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
    b_interval = binance_intervals.get(interval, "15m")
    for b_base in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            url = f"{b_base}/api/v3/klines?symbol={bin_sym}&interval={b_interval}&limit={limit}"
            r = HTTP.get(url, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200:
                raw = r.json()
                if isinstance(raw, list) and len(raw) >= 5:
                    out = []
                    for k in raw:
                        t = int(k[0]) // 1000
                        out.append({
                            "time": t, "close_time": t + gran,
                            "open": float(k[1]), "high": float(k[2]),
                            "low": float(k[3]), "close": float(k[4]),
                            "vol": float(k[5])
                        })
                    out.sort(key=lambda z: z["time"])
                    return out[-limit:]
        except Exception:
            pass

    try:
        delta_sym = params["delta_symbol"]
        end_ts = int(time.time())
        start_ts = end_ts - (limit * gran)
        url = f"https://api.delta.exchange/v2/history/candles?resolution={interval}&symbol={delta_sym}&start={start_ts}&end={end_ts}"
        r = HTTP.get(url, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            raw = r.json().get("result", [])
            if isinstance(raw, list) and len(raw) >= 5:
                out = []
                for k in raw:
                    t = int(k["time"])
                    out.append({
                        "time": t, "close_time": t + gran,
                        "open": float(k["open"]), "high": float(k["high"]),
                        "low": float(k["low"]), "close": float(k["close"]),
                        "vol": float(k.get("volume", 1.0))
                    })
                out.sort(key=lambda z: z["time"])
                return out[-limit:]
    except Exception:
        pass

    now_s = int(time.time())
    default_p = params["default_price"]
    m_dict = get_market(symbol)
    last_p = m_dict.get("price") or default_p
    synth_bars = []
    spread = 3.0 if params["is_gold"] else 35.0
    for idx in range(limit):
        bar_t = now_s - (limit - idx) * gran
        synth_bars.append({
            "time": bar_t, "close_time": bar_t + gran,
            "open": last_p - (spread * 0.3), "high": last_p + spread,
            "low": last_p - spread, "close": last_p + (spread * 0.1),
            "vol": 150.0
        })
    return synth_bars

def extract_pdh_pdl(daily_candles: list) -> tuple:
    if len(daily_candles) >= 2:
        prev_day = daily_candles[-2]
        today_open = float(daily_candles[-1].get("open", daily_candles[-2]["close"]))
        return float(prev_day["high"]), float(prev_day["low"]), today_open
    elif len(daily_candles) == 1:
        return float(daily_candles[0]["high"]), float(daily_candles[0]["low"]), float(daily_candles[0]["open"])
    return None, None, None

def extract_24h_levels(candles_15m: list) -> tuple:
    bars_24h = candles_15m[-96:] if len(candles_15m) >= 96 else candles_15m
    if bars_24h:
        h24 = max(c["high"] for c in bars_24h)
        l24 = min(c["low"] for c in bars_24h)
        vol24 = sum(c.get("vol", 0.0) for c in bars_24h)
        open24 = float(bars_24h[0]["open"])
        return h24, l24, vol24, open24
    return None, None, 0.0, None

def extract_asian_range(candles_15m: list) -> tuple:
    now_dt = datetime.now(timezone.utc)
    today_start_epoch = int(datetime(now_dt.year, now_dt.month, now_dt.day, 0, 0, tzinfo=timezone.utc).timestamp())
    asian_end_epoch = today_start_epoch + (7 * 3600)
    asian_bars = [c for c in candles_15m if today_start_epoch <= c.get("time", 0) <= asian_end_epoch]
    if asian_bars:
        arh = max(c["high"] for c in asian_bars)
        arl = min(c["low"] for c in asian_bars)
        return arh, arl
    return None, None

def approx_poc(cs: List[Dict[str, float]], bins: int = 30) -> Optional[float]:
    if not cs:
        return None
    lo = min(c["low"] for c in cs)
    hi = max(c["high"] for c in cs)
    if hi <= lo:
        return lo
    width = (hi - lo) / bins
    vol = [0.0] * bins
    for c in cs:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        idx = min(bins - 1, max(0, int((typical - lo) / width)))
        vol[idx] += c["vol"]
    max_idx = max(range(bins), key=lambda n: vol[n])
    return lo + (max_idx + 0.5) * width

def refresh_symbol_market_data(symbol: str, lock) -> bool:
    try:
        p = fetch_price(symbol)
        params = config.get_asset_params(symbol)
        if p is None:
            with lock:
                p = get_market(symbol).get("price") or params["default_price"]

        sess, in_kz, kz_name, is_dead = session_and_killzone()
        news_block = check_economic_news_blackout()
        funding = fetch_funding_rate(symbol)

        with lock:
            m = get_market(symbol)
            m["price"] = p
            m["price_ts"] = time.time()
            m["session"] = sess
            m["in_killzone"] = in_kz
            m["killzone_name"] = kz_name
            m["is_dead_hour"] = is_dead
            m["news_blackout"] = news_block
            m["funding_rate"] = funding

        ks = {}
        for k in ("15m", "1h", "4h", "1d"):
            bars = fetch_klines(symbol, k, 120)
            if len(bars) < 5:
                if "15m" in ks and len(ks["15m"]) >= 10:
                    bars = ks["15m"]
                else:
                    bars = fetch_klines(symbol, k, 120)
            ks[k] = bars
            time.sleep(0.02)

        pdh, pdl, d_open = extract_pdh_pdl(ks.get("1d", []))
        h24, l24, vol24, open24 = extract_24h_levels(ks.get("15m", []))
        if d_open is None:
            d_open = open24
        arh, arl = extract_asian_range(ks.get("15m", []))
        poc = approx_poc(ks.get("15m", [])[-96:])
        cvd = calc_volume_delta(ks.get("15m", []))

        with lock:
            m = get_market(symbol)
            m.update(
                klines=ks, klines_ts=time.time(), poc=poc, pdh=pdh, pdl=pdl,
                daily_open=d_open, day_high_24h=h24, day_low_24h=l24,
                asian_high=arh, asian_low=arl, cvd_ratio=cvd, error=None
            )
        return True
    except Exception as e:
        with lock:
            get_market(symbol)["error"] = str(e)[:250]
        log.warning("Market refresh failed for %s: %s", symbol, e)
        return False

def refresh_all_market_data(lock) -> bool:
    success = True
    for sym in config.SYMBOLS:
        if not refresh_symbol_market_data(sym, lock):
            success = False
    return success

def refresh_market_data(lock) -> bool:
    return refresh_all_market_data(lock)
