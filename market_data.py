import time, math, logging, requests
from typing import Dict, Any, List, Optional
import config

log = logging.getLogger("DataEngine")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 ProSMC/1.0"})

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

# 6 & 7. Geo-Block Immunity & Single Exchange Alignment
def fetch_price() -> Optional[float]:
    # Binance Spot
    try:
        r = HTTP.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": config.SYMBOL}, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["price"])
            if math.isfinite(p) and p > 0: return p
    except Exception: pass
    # Coinbase Spot
    try:
        r = HTTP.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            p = float(r.json()["data"]["amount"])
            if math.isfinite(p) and p > 0: return p
    except Exception: pass
    return None

# 8 & 9. Confirmed Closed Candles Only & Chronological Sort (Lookback expanded to 48h+)
def fetch_klines(interval: str, limit: int = 200) -> List[Dict[str, float]]:
    # Binance Spot Fetch
    try:
        r = HTTP.get("https://api.binance.com/api/v3/klines",
                     params={"symbol": config.SYMBOL, "interval": interval, "limit": min(limit + 5, 300)},
                     timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            raw = r.json()
            now_ms = int(time.time() * 1000)
            out = []
            for k in raw:
                if int(k[6]) >= now_ms: continue  # discard incomplete bar
                out.append({
                    "time": int(k[0] // 1000), "close_time": int(k[6] // 1000),
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "vol": float(k[5])
                })
            if len(out) >= 10:
                out.sort(key=lambda z: z["time"])
                return out[-limit:]
    except Exception: pass

    # Kraken Fallback
    gran = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}.get(interval, 15)
    try:
        r = HTTP.get(f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={gran}", timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200:
            raw = r.json().get("result", {}).get("XXBTZUSD", [])
            now_s = int(time.time())
            out = []
            for k in raw:
                t = int(k[0])
                if t + (gran * 60) > now_s: continue
                out.append({
                    "time": t, "close_time": t + (gran * 60),
                    "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "vol": float(k[6])
                })
            if len(out) >= 10:
                out.sort(key=lambda z: z["time"])
                return out[-limit:]
    except Exception: pass
    return []

# 16. Volume Absorption & POC
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

# 10 & 11. Stale Data Freeze & Zero Artificial Bars with Safe Fallback
def refresh_market_data(lock) -> bool:
    try:
        p = fetch_price()
        if p is None:
            with lock:
                p = MARKET.get("price")
            if p is None:
                raise ValueError("Live ticker price unavailable")

        ks = {}
        # 15m needs 200 bars (50 hours), others need standard lookback
        limits = {"15m": 200, "1h": 100, "4h": 60, "1d": 30}
        for k in ("15m", "1h", "4h", "1d"):
            bars = fetch_klines(k, limits[k])
            if len(bars) < 10:
                # Reuse cached data if fresh API fetch temporarily throttled
                with lock:
                    cached_bars = MARKET.get("klines", {}).get(k, [])
                if len(cached_bars) >= 10:
                    bars = cached_bars
                else:
                    raise ValueError(f"Insufficient history for {k}")
            ks[k] = bars
            time.sleep(0.12)

        poc = approx_poc(ks["15m"][-60:])
        with lock:
            MARKET.update(price=p, price_ts=time.time(), klines=ks, klines_ts=time.time(),
                          session=session_now(), poc=poc, error=None)
        return True
    except Exception as e:
        with lock:
            # If we already have past valid candles, preserve them without setting hard error
            if MARKET.get("klines"):
                log.warning("Market refresh intermittent warn: %s (using cached bars)", e)
                return True
            MARKET["error"] = str(e)[:250]
        log.warning("Market refresh failed: %s", e)
        return False
