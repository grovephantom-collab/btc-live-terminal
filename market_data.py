import time, math, logging, requests
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import config

log = logging.getLogger("DataEngine")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 QuantTerminal/4.0"})

MARKET: Dict[str, Any] = {
    "price": None, "price_ts": 0, "klines": {}, "klines_ts": 0,
    "session": "INITIALIZING", "in_killzone": False, "killzone_name": "OFF_HOURS",
    "poc": None, "funding_rate": 0.0001,
    "pdh": None, "pdl": None, "daily_open": None,
    "day_high_24h": None, "day_low_24h": None,
    "asian_high": None, "asian_low": None,
    "news_blackout": False, "error": None
}

def session_and_killzone() -> tuple:
    now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    m = now_utc.minute
    total_m = h * 60 + m

    session_name = "SESSION CLOSE"
    in_kz = False
    kz_name = "OFF_HOURS"

    if h < 7:
        session_name = "ASIAN RANGE"
    elif h < 13:
        session_name = "LONDON EXPANSION"
    elif h < 21:
        session_name = "NEW YORK TREND"

    # London Open Killzone: 07:00 - 10:00 UTC (12:30 PM - 03:30 PM IST)
    if 420 <= total_m <= 600:
        in_kz = True
        kz_name = "LONDON KILLZONE"
    # New York Open Killzone: 12:30 - 16:30 UTC (06:00 PM - 10:00 PM IST)
    elif 750 <= total_m <= 990:
        in_kz = True
        kz_name = "NEW YORK KILLZONE"

    return session_name, in_kz, kz_name

def check_economic_news_blackout() -> bool:
    now_utc = datetime.now(timezone.utc)
    minute_of_day = now_utc.hour * 60 + now_utc.minute
    if now_utc.weekday() in [2, 3]: # Wed/Thu
        cpi_m = 12 * 60 + 30 # 12:30 UTC
        fomc_m = 18 * 60     # 18:00 UTC
        if abs(minute_of_day - cpi_m) <= 25 or abs(minute_of_day - fomc_m) <= 25:
            return True
    return False

def fetch_funding_rate() -> float:
    try:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
        r = HTTP.get(url, timeout=3.5)
        if r.status_code == 200:
            return float(r.json().get("lastFundingRate", 0.0001))
    except Exception:
        pass
    try:
        url = "https://api.delta.exchange/v2/tickers/BTCUSD"
        r = HTTP.get(url, timeout=3.5)
        if r.status_code == 200:
            return float(r.json().get("result", {}).get("funding_rate", 0.0001))
    except Exception:
        pass
    return 0.0001

def fetch_price() -> Optional[float]:
    try:
        r = HTTP.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["data"]["amount"])
            if math.isfinite(p) and p > 0:
                return p
    except Exception:
        pass

    try:
        r = HTTP.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": config.SYMBOL}, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["price"])
            if math.isfinite(p) and p > 0:
                return p
    except Exception:
        pass
    
    try:
        url = "https://api.delta.exchange/v2/tickers/BTCUSD"
        r = HTTP.get(url, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json().get("result", {}).get("mark_price", 0))
            if math.isfinite(p) and p > 0:
                return p
    except Exception:
        pass

    return None

def fetch_klines(interval: str, limit: int = 120) -> List[Dict[str, float]]:
    gran_map = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    gran = gran_map.get(interval, 900)
    
    # Try Coinbase first
    try:
        url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={gran}"
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

    # Fallback to Binance
    binance_intervals = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
    b_interval = binance_intervals.get(interval, "15m")
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={config.SYMBOL}&interval={b_interval}&limit={limit}"
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

    now_s = int(time.time())
    last_p = MARKET.get("price") or 76500.0
    synth_bars = []
    for idx in range(limit):
        bar_t = now_s - (limit - idx) * gran
        synth_bars.append({
            "time": bar_t, "close_time": bar_t + gran,
            "open": last_p - 10.0, "high": last_p + 35.0,
            "low": last_p - 35.0, "close": last_p + 5.0,
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
    """Computes rolling 24-hour High, Low, and Open from 96 candles of 15m."""
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

def refresh_market_data(lock) -> bool:
    try:
        p = fetch_price()
        if p is None:
            p = MARKET.get("price") or 76500.0

        sess, in_kz, kz_name = session_and_killzone()
        news_block = check_economic_news_blackout()
        funding = fetch_funding_rate()

        with lock:
            MARKET["price"] = p
            MARKET["price_ts"] = time.time()
            MARKET["session"] = sess
            MARKET["in_killzone"] = in_kz
            MARKET["killzone_name"] = kz_name
            MARKET["news_blackout"] = news_block
            MARKET["funding_rate"] = funding

        ks = {}
        for k in ("15m", "1h", "4h", "1d"):
            bars = fetch_klines(k, 120)
            if len(bars) < 5:
                if "15m" in ks and len(ks["15m"]) >= 10:
                    bars = ks["15m"]
                else:
                    bars = fetch_klines(k, 120)
            ks[k] = bars
            time.sleep(0.02)

        pdh, pdl, d_open = extract_pdh_pdl(ks.get("1d", []))
        h24, l24, vol24, open24 = extract_24h_levels(ks.get("15m", []))
        if d_open is None:
            d_open = open24
        arh, arl = extract_asian_range(ks.get("15m", []))
        poc = approx_poc(ks.get("15m", [])[-96:])

        with lock:
            MARKET.update(
                klines=ks,
                klines_ts=time.time(),
                poc=poc,
                pdh=pdh,
                pdl=pdl,
                daily_open=d_open,
                day_high_24h=h24,
                day_low_24h=l24,
                asian_high=arh,
                asian_low=arl,
                error=None
            )
        return True
    except Exception as e:
        with lock:
            MARKET["error"] = str(e)[:250]
        log.warning("Market refresh failed: %s", e)
        return False
