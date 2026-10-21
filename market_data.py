import time, math, logging, requests
from typing import Dict, Any, List, Optional
import config

log = logging.getLogger("DataEngine")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 ProSMC/2.0"})

MARKET: Dict[str, Any] = {
    "price": None, "price_ts": 0, "klines": {}, "klines_ts": 0,
    "session": "INITIALIZING", "poc": None, "error": None
}

def session_now() -> str:
    from datetime import datetime, timezone
    h = datetime.now(timezone.utc).hour
    if h < 7: return "ASIAN RANGE"
    if h < 13: return "LONDON EXPANSION"
    if h < 21: return "NEW YORK TREND"
    return "SESSION CLOSE"

def fetch_price() -> Optional[float]:
    try:
        r = HTTP.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["data"]["amount"])
            if math.isfinite(p) and p > 0: return p
    except Exception: pass

    try:
        r = HTTP.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": config.SYMBOL}, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["price"])
            if math.isfinite(p) and p > 0: return p
    except Exception: pass
    return None

def fetch_klines(interval: str, limit: int = 200) -> List[Dict[str, float]]:
    gran_map = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    gran = gran_map.get(interval, 900)
    try:
        url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={gran}"
        r = HTTP.get(url, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            raw = r.json()
            if isinstance(raw, list) and len(raw) >= 5:
                now_s = int(time.time())
                out = []
                for k in raw:
                    t = int(k[0])
                    if t + gran > now_s: continue
                    out.append({"time": t, "close_time": t + gran, "open": float(k[3]), "high": float(k[2]), "low": float(k[1]), "close": float(k[4]), "vol": float(k[5])})
                out.sort(key=lambda z: z["time"])
                return out[-limit:]
    except Exception: pass
    return []

def approx_poc(cs: List[Dict[str, float]], bins: int = 30) -> Optional[float]:
    if not cs: return None
    lo = min(c["low"] for c in cs)
    hi = max(c["high"] for c in cs)
    if hi <= lo: return lo
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
        if p is None: raise ValueError("Live ticker price unavailable")
        with lock:
            MARKET["price"] = p
            MARKET["price_ts"] = time.time()
            MARKET["session"] = session_now()

        ks = {}
        limits = {"15m": 200, "1h": 100, "4h": 60, "1d": 30}
        for k in ("15m", "1h", "4h", "1d"):
            bars = fetch_klines(k, limits[k])
            if len(bars) < 15:
                cached = MARKET.get("klines", {}).get(k, [])
                if len(cached) >= 15: bars = cached
                else: raise ValueError(f"Insufficient history for {k}")
            ks[k] = bars
            time.sleep(0.08)

        poc = approx_poc(ks["15m"][-60:])
        with lock:
            MARKET.update(klines=ks, klines_ts=time.time(), poc=poc, error=None)
        return True
    except Exception as e:
        with lock: MARKET["error"] = str(e)[:250]
        log.warning("Market refresh failed: %s", e)
        return False
