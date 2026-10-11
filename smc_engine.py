import math
from typing import List, Dict, Optional, Any
import config

def ema(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period: return None
    k = 2 / (period + 1)
    v = sum(values[:period]) / period
    for x in values[period:]: v += k * (x - v)
    return v

def atr(cs: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(cs) < period + 1: return None
    tr = [max(cs[i]["high"] - cs[i]["low"], abs(cs[i]["high"] - cs[i-1]["close"]), abs(cs[i]["low"] - cs[i-1]["close"]))
          for i in range(1, len(cs))]
    return sum(tr[-period:]) / period

# 15. Candle Anatomy & Psychology
def psyche(c: Dict[str, float]) -> Dict[str, float]:
    rng = max(c["high"] - c["low"], 1e-9)
    return {
        "body": abs(c["close"] - c["open"]) / rng,
        "upper": (c["high"] - max(c["open"], c["close"])) / rng,
        "lower": (min(c["open"], c["close"]) - c["low"]) / rng
    }

def swings(cs: List[Dict[str, float]], n: int = 3):
    hs, ls = [], []
    for i in range(n, len(cs) - n):
        c = cs[i]
        if all(c["high"] > cs[i-j]["high"] for j in range(1, n+1)) and all(c["high"] >= cs[i+j]["high"] for j in range(1, n+1)):
            hs.append({"price": c["high"], "time": c["time"]})
        if all(c["low"] < cs[i-j]["low"] for j in range(1, n+1)) and all(c["low"] <= cs[i+j]["low"] for j in range(1, n+1)):
            ls.append({"price": c["low"], "time": c["time"]})
    return hs, ls

# 17. Fair Value Gap (FVG) CE Retest Detection
def fvgs(cs: List[Dict[str, float]]) -> List[Dict[str, Any]]:
    out = []
    for i in range(1, len(cs) - 1):
        a, m, b = cs[i-1], cs[i], cs[i+1]
        rng = max(m["high"] - m["low"], 1e-9)
        if abs(m["close"] - m["open"]) / rng < 0.50: continue
        if b["low"] > a["high"] and b["low"] - a["high"] >= config.MIN_GAP:
            out.append({"direction": "LONG", "top": b["low"], "bottom": a["high"], "ce": (b["low"] + a["high"]) / 2, "time": m["time"]})
        elif a["low"] > b["high"] and a["low"] - b["high"] >= config.MIN_GAP:
            out.append({"direction": "SHORT", "top": a["low"], "bottom": b["high"], "ce": (a["low"] + b["high"]) / 2, "time": m["time"]})
    return out

# 12 & 13. Multi-Timeframe Structure & Dealing Range Equilibrium
def get_structure(ks: Dict[str, List[Dict[str, float]]]) -> Dict[str, Any]:
    c15, c1h, c4h, c1d = ks["15m"], ks["1h"], ks["4h"], ks["1d"]
    e20 = ema([c["close"] for c in c4h], 20)
    e50 = ema([c["close"] for c in c4h], 50)
    bias = "BULLISH" if (e20 and e50 and e20 > e50) else "BEARISH" if (e20 and e50 and e20 < e50) else "NEUTRAL"
    sh, sl = swings(c15)
    sw_hi = sh[-1]["price"] if sh else max(c["high"] for c in c15[-30:])
    sw_lo = sl[-1]["price"] if sl else min(c["low"] for c in c15[-30:])
    return {
        "bias4h": bias, "swing_high": sw_hi, "swing_low": sw_lo,
        "pdh": c1d[-1]["high"] if c1d else sw_hi, "pdl": c1d[-1]["low"] if c1d else sw_lo,
        "eq": (sw_hi + sw_lo) / 2, "atr": atr(c15), "fvgs": fvgs(c15)
    }

# 14 & 18. Liquidity Reclaim & Session Window Enforcement
def evaluate_signal(st: Dict[str, Any], cs: List[Dict[str, float]], price: float, sess: str, poc: Optional[float]) -> Optional[Dict[str, Any]]:
    if len(cs) < 30 or not st["atr"] or sess not in config.ALLOWED_SESSIONS: return None
    swept, trigger = cs[-2], cs[-1]
    p = psyche(swept)
    eq = st["eq"]
    cands = []

    # SSL Sweep & Reclaim -> LONG in Discount
    for label, level in [("PDL", st["pdl"]), ("SWING_LOW", st["swing_low"])]:
        if (swept["low"] < level - config.SWEEP_BUFFER and trigger["close"] > level and trigger["close"] > trigger["open"]
            and price < eq and p["lower"] >= 0.35 and p["body"] <= 0.65 and st["bias4h"] != "BEARISH"):
            sl = min(swept["low"], trigger["low"]) - max(config.SL_BUFFER, 0.15 * st["atr"])
            risk = price - sl
            if config.MIN_STOP <= risk <= config.MAX_STOP:
                score = min(100, 70 + min(12, p["lower"] * 15) + (8 if label == "PDL" else 4) + (3 if poc and price < poc else 0))
                cands.append({"direction": "LONG", "setup_name": f"{label} SSL sweep/reclaim", "entry": price, "sl": sl, "risk": risk, "score": score, "swept_level": label, "trigger_time": trigger["time"]})

    # BSL Trap & Reclaim -> SHORT in Premium
    for label, level in [("PDH", st["pdh"]), ("SWING_HIGH", st["swing_high"])]:
        if (swept["high"] > level + config.SWEEP_BUFFER and trigger["close"] < level and trigger["close"] < trigger["open"]
            and price > eq and p["upper"] >= 0.35 and p["body"] <= 0.65 and st["bias4h"] != "BULLISH"):
            sl = max(swept["high"], trigger["high"]) + max(config.SL_BUFFER, 0.15 * st["atr"])
            risk = sl - price
            if config.MIN_STOP <= risk <= config.MAX_STOP:
                score = min(100, 70 + min(12, p["upper"] * 15) + (8 if label == "PDH" else 4) + (3 if poc and price > poc else 0))
                cands.append({"direction": "SHORT", "setup_name": f"{label} BSL sweep/reclaim", "entry": price, "sl": sl, "risk": risk, "score": score, "swept_level": label, "trigger_time": trigger["time"]})

    # FVG CE Mitigation
    for f in st.get("fvgs", []):
        if f["direction"] == "LONG" and price < eq and st["bias4h"] == "BULLISH":
            if trigger["low"] <= f["ce"] and trigger["close"] > f["ce"] and trigger["close"] > trigger["open"]:
                sl = f["bottom"] - max(config.SL_BUFFER, 0.15 * st["atr"])
                risk = price - sl
                if config.MIN_STOP <= risk <= config.MAX_STOP:
                    cands.append({"direction": "LONG", "setup_name": "FVG CE Retest in Discount", "entry": price, "sl": sl, "risk": risk, "score": 80.0, "swept_level": "FVG_CE", "trigger_time": trigger["time"]})
        elif f["direction"] == "SHORT" and price > eq and st["bias4h"] == "BEARISH":
            if trigger["high"] >= f["ce"] and trigger["close"] < f["ce"] and trigger["close"] < trigger["open"]:
                sl = f["top"] + max(config.SL_BUFFER, 0.15 * st["atr"])
                risk = sl - price
                if config.MIN_STOP <= risk <= config.MAX_STOP:
                    cands.append({"direction": "SHORT", "setup_name": "FVG CE Retest in Premium", "entry": price, "sl": sl, "risk": risk, "score": 80.0, "swept_level": "FVG_CE", "trigger_time": trigger["time"]})

    if not cands: return None
    s = max(cands, key=lambda x: x["score"])
    if s["score"] < config.MIN_SCORE: return None
    sign = 1 if s["direction"] == "LONG" else -1
    s.update(tp1=s["entry"] + sign * 1.5 * s["risk"], tp2=s["entry"] + sign * 2.25 * s["risk"], tp3=s["entry"] + sign * 3.0 * s["risk"], timeframe="15m", session=sess)
    return s
