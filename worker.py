import os
import time
import math
import sqlite3
import logging
import traceback
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

# ============================================================
# BTCUSDT SMC PRODUCTION WORKER + REST API ENGINE (RENDER)
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__)

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

NORMAL_MIN = 70
NORMAL_MAX = 84
EVENT_MIN = 85
EVENT_COOLDOWN = 12 * 60 * 60
BASE = "https://fapi.binance.com"

LOCK = threading.RLock()

# Runtime Engine State accessible by Flask API
ENGINE_STATUS = {
    "boot_time": datetime.now(timezone.utc).isoformat(),
    "last_scan_time": None,
    "last_price": 0.0,
    "worker_alive": True,
    "scans_completed": 0
}

GLOBAL_ACTIVE = {
    "15M": None,
    "1H": None,
    "4H": None,
    "EVENT": None
}

# ---------------- 1. DATABASE ----------------
def db():
    c = sqlite3.connect(DB_FILE, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, created_at TEXT, signal_type TEXT, tf TEXT, direction TEXT,
        setup TEXT, score INTEGER, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        exit REAL, result TEXT, pnl_r REAL, confluence TEXT, status TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS event_locks(
        event_key TEXT PRIMARY KEY, created_at TEXT, direction TEXT,
        score INTEGER, status TEXT)""")
    c.commit(); c.close()

init_db()

# ---------------- 2. FLASK API ROUTES (PROPER DICT FORMAT) ----------------
@app.route("/")
@app.route("/healthz")
def health():
    return "BTCUSDT SMC Master Engine Running 24/7", 200

@app.route("/status")
def api_status():
    return jsonify({
        "status": "healthy" if ENGINE_STATUS["worker_alive"] else "degraded",
        "last_scan": ENGINE_STATUS["last_scan_time"],
        "last_price": ENGINE_STATUS["last_price"],
        "scans": ENGINE_STATUS["scans_completed"],
        "boot_time": ENGINE_STATUS["boot_time"]
    }), 200

@app.route("/active-trades")
def api_active():
    with LOCK:
        active_list = []
        for k, t in GLOBAL_ACTIVE.items():
            if t:
                active_list.append({
                    "id": t["id"],
                    "tf": t["tf"],
                    "dir": t["dir"],
                    "entry": float(t["entry"]),
                    "sl": float(t["sl"]),
                    "tp1": float(t["tp1"]),
                    "tp2": float(t["tp2"]),
                    "tp3": float(t["tp3"]),
                    "setup": t["setup"],
                    "score": int(t["score"])
                })
        return jsonify(active_list), 200

@app.route("/vault-data")
def api_vault_data():
    try:
        c = db()
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT created_at, signal_type, tf, direction, setup, score, entry, exit, result, pnl_r, status 
            FROM trades ORDER BY id DESC LIMIT 50
        """).fetchall()
        c.close()

        formatted_vault = []
        for r in rows:
            formatted_vault.append({
                "time": r["created_at"],
                "type": r["signal_type"],
                "tf": r["tf"],
                "dir": r["direction"],
                "setup": r["setup"],
                "score": r["score"],
                "entry": float(r["entry"] or 0.0),
                "exit": float(r["exit"] or 0.0),
                "res": r["result"],
                "pnl": float(r["pnl_r"] or 0.0),
                "status": r["status"]
            })

        # Structured active trades
        with LOCK:
            active_list = []
            for k, t in GLOBAL_ACTIVE.items():
                if t:
                    active_list.append({
                        "id": t["id"],
                        "tf": t["tf"],
                        "dir": t["dir"],
                        "entry": float(t["entry"]),
                        "sl": float(t["sl"]),
                        "tp1": float(t["tp1"]),
                        "tp2": float(t["tp2"]),
                        "tp3": float(t["tp3"]),
                        "setup": t["setup"],
                        "score": int(t["score"])
                    })

        return jsonify({
            "vault": formatted_vault,
            "active": active_list
        }), 200
    except Exception as e:
        logging.error(f"Error in /vault-data: {e}")
        return jsonify({"vault": [], "active": []}), 500

# ---------------- 3. TELEGRAM ----------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logging.error(f"Telegram dispatch failed: {e}")

def db_upsert_trade(t, status="OPEN", exit_p=0.0, res="RUNNING", pnl=0.0):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, confluence, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], t["score"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], exit_p, res, pnl, ", ".join(t.get("reasons", [])), status))
        c.commit(); c.close()
    except Exception as e:
        logging.error(f"DB Upsert Error: {e}")

# ---------------- 4. MARKET DATA FETCHER ----------------
def futures_get(path, params=None, timeout=4):
    try:
        r = requests.get(BASE + path, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def fetch_klines(interval, limit=300):
    raw = futures_get("/fapi/v1/klines", {"symbol": SYMBOL, "interval": interval, "limit": limit})
    if not isinstance(raw, list):
        return []
    return [{
        "time": int(b[0] // 1000), "open": float(b[1]), "high": float(b[2]),
        "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])
    } for b in raw[:-1]]

def fetch_price():
    x = futures_get("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    try: return float(x["price"])
    except Exception: return 0.0

def fetch_funding_rate():
    try:
        x = futures_get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
        return float(x.get("lastFundingRate", 0.0))
    except Exception:
        return 0.0

def fetch_oi_hist():
    try:
        raw = futures_get("/futures/data/openInterestHist", {"symbol": SYMBOL, "period": "15m", "limit": 5})
        if isinstance(raw, list) and len(raw) >= 2:
            cur = float(raw[-1]["sumOpenInterest"])
            prev = float(raw[-2]["sumOpenInterest"])
            return ((cur - prev) / prev) if prev > 0 else 0.0
    except Exception:
        pass
    return 0.0

# ---------------- 5. QUANT & SMC INDICATORS ----------------
def calculate_rsi(candles, period=14):
    if len(candles) < period + 2: return 50.0
    closes = [c["close"] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr(candles, period=14):
    if len(candles) < period + 1: return 120.0
    trs = [max(candles[i]["high"] - candles[i]["low"], 
               abs(candles[i]["high"] - candles[i-1]["close"]), 
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

def volume_ratio(candles, period=20):
    if len(candles) < period + 1: return 1.0
    avg = sum(x["vol"] for x in candles[-period - 1:-1]) / period
    return (candles[-1]["vol"] / avg) if avg > 0 else 1.0

def volume_profile_poc(candles, lookback=60, bin_size=25.0):
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    bins = {}
    for c in recent:
        lo = math.floor(c["low"] / bin_size) * bin_size
        hi = math.ceil(c["high"] / bin_size) * bin_size
        steps = max(1, int(round((hi - lo) / bin_size)))
        share = c["vol"] / steps
        for k in range(steps):
            bins[lo + k * bin_size] = bins.get(lo + k * bin_size, 0.0) + share
    return max(bins, key=bins.get) if bins else recent[-1]["close"]

def channel_break(candles, lookback=30):
    if len(candles) < lookback + 5: return None
    r = candles[-lookback:]
    xs = list(range(len(r)))
    ys = [c["close"] for c in r]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    den = sum((x-mx)**2 for x in xs)
    slope = sum((x-mx)*(y-my) for x,y in zip(xs,ys))/den if den else 0
    intercept = my - slope*mx
    residuals = [y-(intercept+slope*x) for x,y in zip(xs,ys)]
    width = max(abs(x) for x in residuals) if residuals else 0
    center = intercept + slope*(len(r)-1)
    if r[-1]["close"] > center + width: return "LONG"
    if r[-1]["close"] < center - width: return "SHORT"
    return None

def fib_pocket(candles, lookback=80):
    if len(candles) < lookback: return None
    r = candles[-lookback:]
    hi, lo = max(x["high"] for x in r), min(x["low"] for x in r)
    d = hi - lo
    if d <= 0: return None
    cur = candles[-1]
    b618, b786 = hi - 0.618 * d, hi - 0.786 * d
    if b786 <= cur["low"] <= b618 and cur["close"] > b786:
        return {"dir": "LONG", "sl_ref": lo}
    s618, s786 = lo + 0.618 * d, lo + 0.786 * d
    if s618 <= cur["high"] <= s786 and cur["close"] < s786:
        return {"dir": "SHORT", "sl_ref": hi}
    return None

def confirmed_swings(candles, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h, l = candles[i]["high"], candles[i]["low"]
        if all(h > candles[j]["high"] for j in range(i-left, i)) and all(h >= candles[j]["high"] for j in range(i+1, i+right+1)):
            highs.append((i, h))
        if all(l < candles[j]["low"] for j in range(i-left, i)) and all(l <= candles[j]["low"] for j in range(i+1, i+right+1)):
            lows.append((i, l))
    return highs, lows

def smc_structure(candles):
    if len(candles) < 25:
        return {"bias": "NEUTRAL", "bos": None, "choch": None, "last_high": None, "last_low": None}
    highs, lows = confirmed_swings(candles)
    if not highs or not lows:
        return {"bias": "NEUTRAL", "bos": None, "choch": None, "last_high": None, "last_low": None}
    last_h = highs[-1][1]; last_l = lows[-1][1]
    close = candles[-1]["close"]
    bias = "NEUTRAL"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]: bias = "BULL"
        elif highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]: bias = "BEAR"
    bos, choch = None, None
    if bias == "BULL":
        if close > last_h: bos = "LONG"
        if close < last_l: choch = "SHORT"
    elif bias == "BEAR":
        if close < last_l: bos = "SHORT"
        if close > last_h: choch = "LONG"
    else:
        if close > last_h: bos = "LONG"
        elif close < last_l: bos = "SHORT"
    return {"bias": bias, "bos": bos, "choch": choch, "last_high": last_h, "last_low": last_l}

def liquidity_sweep(candles, lookback=10):
    if len(candles) < lookback + 2: return None
    ref = candles[-lookback-1:-1]
    cur = candles[-1]
    hi, lo = max(x["high"] for x in ref), min(x["low"] for x in ref)
    if cur["low"] < lo and cur["close"] > lo: return {"dir": "LONG", "level": lo}
    if cur["high"] > hi and cur["close"] < hi: return {"dir": "SHORT", "level": hi}
    return None

def detect_institutional_fvg(candles):
    if len(candles) < 3: return None
    c1, _, c3 = candles[-3], candles[-2], candles[-1]
    if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) > 15.0:
        return {"dir": "LONG", "low": c1["high"], "high": c3["low"]}
    if c3["high"] < c1["low"] and (c1["low"] - c3["high"]) > 15.0:
        return {"dir": "SHORT", "low": c3["high"], "high": c1["low"]}
    return None

def evaluate_score(direction, d):
    pts = 0; why = []
    if d.get("sweep") == direction: pts += 20; why.append("Liquidity Sweep (+20)")
    if d.get("htf_bias") == ("BULL" if direction == "LONG" else "BEAR"): pts += 15; why.append("HTF Bias Alignment (+15)")
    if d.get("structure") == direction: pts += 15; why.append("BOS / CHoCH Trigger (+15)")
    if d.get("fvg") == direction: pts += 10; why.append("Institutional FVG (+10)")
    if d.get("displacement"): pts += 10; why.append("Displacement Candle (+10)")
    if d.get("volume", 1.0) >= 1.25: pts += 10; why.append("Volume Expansion (+10)")
    if d.get("oi_change", 0.0) >= 0.005: pts += 10; why.append("15M OI Inflow (+10)")
    if d.get("funding_ok"): pts += 8; why.append("Contrarian Funding (+8)")
    if d.get("rsi_ok"): pts += 7; why.append("RSI Momentum (+7)")
    return min(105, pts), why

# ---------------- 6. ENGINE CONTROLLER (15M / 1H / 4H + EVENT) ----------------
class AutonomousEngine:
    def __init__(self):
        self.event_lock_until = 0
        self.last_signal_time = {"15M": 0, "1H": 0, "4H": 0}
        self.recover_state()

    def recover_state(self):
        try:
            c = db()
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
            c.close()
            with LOCK:
                for r in rows:
                    t = {
                        "id": r["trade_id"], "created": r["created_at"], "type": r["signal_type"], "tf": r["tf"], "dir": r["direction"],
                        "setup": r["setup"], "score": r["score"], "entry": r["entry"], "sl": r["sl"],
                        "tp1": r["tp1"], "tp2": r["tp2"], "tp3": r["tp3"],
                        "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False, "reasons": []
                    }
                    if t["type"] == "EVENT": GLOBAL_ACTIVE["EVENT"] = t
                    else: GLOBAL_ACTIVE[t["tf"]] = t
            logging.info(f"State recovered: {len(rows)} active trades loaded.")
        except Exception as e:
            logging.error(f"State recovery error: {e}")

    def manage_positions(self, p, s4):
        with LOCK:
            for tf in ["15M", "1H", "4H"]:
                t = GLOBAL_ACTIVE[tf]
                if not t: continue
                if t["dir"] == "LONG":
                    if p >= t["tp1"] and not t["tp1_hit"]:
                        t["tp1_hit"] = True; t["sl"] = t["entry"]; t["be"] = True
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP1 HIT (+1R)</b>\n33% booked. SL locked to BE (${t['entry']:,.2f})")
                    elif p >= t["tp2"] and not t["tp2_hit"]:
                        t["tp2_hit"] = True; t["sl"] = t["tp1"]
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP2 HIT (+2R)</b>\n33% booked. SL trailed to TP1 (${t['tp1']:,.2f})")
                    if p <= t["sl"]:
                        res = "BE EXIT" if t["be"] else "SL HIT"
                        pnl = 0.5 if t["tp1_hit"] else -1.0
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[{tf} NORMAL] {res}</b> @ ${p:,.2f} ({pnl:+.1f}R)")
                        GLOBAL_ACTIVE[tf] = None
                    elif p >= t["tp3"]:
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", pnl=3.0)
                        send_telegram(f"🔥 <b>[{tf} NORMAL] FULL TP3 REACHED!</b> @ ${p:,.2f} (+3.0R)")
                        GLOBAL_ACTIVE[tf] = None
                elif t["dir"] == "SHORT":
                    if p <= t["tp1"] and not t["tp1_hit"]:
                        t["tp1_hit"] = True; t["sl"] = t["entry"]; t["be"] = True
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP1 HIT (+1R)</b>\n33% booked. SL locked to BE (${t['entry']:,.2f})")
                    elif p <= t["tp2"] and not t["tp2_hit"]:
                        t["tp2_hit"] = True; t["sl"] = t["tp1"]
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP2 HIT (+2R)</b>\n33% booked. SL trailed to TP1 (${t['tp1']:,.2f})")
                    if p >= t["sl"]:
                        res = "BE EXIT" if t["be"] else "SL HIT"
                        pnl = 0.5 if t["tp1_hit"] else -1.0
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[{tf} NORMAL] {res}</b> @ ${p:,.2f} ({pnl:+.1f}R)")
                        GLOBAL_ACTIVE[tf] = None
                    elif p <= t["tp3"]:
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", pnl=3.0)
                        send_telegram(f"🔥 <b>[{tf} NORMAL] FULL TP3 REACHED!</b> @ ${p:,.2f} (+3.0R)")
                        GLOBAL_ACTIVE[tf] = None

            ev = GLOBAL_ACTIVE["EVENT"]
            if ev:
                if ev["dir"] == "LONG":
                    if p >= ev["tp1"] and not ev["be"]:
                        ev["be"] = True; ev["sl"] = ev["entry"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP1 HIT</b> -> SL locked to BE (${ev['entry']:,.2f})")
                    if p >= ev["tp3"] and not ev.get("runner_active"):
                        ev["runner_active"] = True
                        send_telegram(f"🔥 <b>[EVENT] TP3 REACHED</b> -> Partials booked. 4H Trail active!")
                    if ev.get("runner_active") and s4.get("last_low") and s4["last_low"] > ev["sl"]:
                        ev["sl"] = s4["last_low"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"🛡 <b>[EVENT 4H TRAIL]</b> SL raised to ${ev['sl']:,.2f}")
                    if p <= ev["sl"]:
                        res = "4H TRAIL EXIT" if ev.get("runner_active") else ("BE EXIT" if ev["be"] else "EVENT SL HIT")
                        pnl = 4.5 if ev.get("runner_active") else (0.0 if ev["be"] else -1.0)
                        db_upsert_trade(ev, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[EVENT FINISHED] {res}</b> @ ${p:,.2f} ({pnl:+.1f}R)")
                        GLOBAL_ACTIVE["EVENT"] = None
                elif ev["dir"] == "SHORT":
                    if p <= ev["tp1"] and not ev["be"]:
                        ev["be"] = True; ev["sl"] = ev["entry"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP1 HIT</b> -> SL locked to BE (${ev['entry']:,.2f})")
                    if p <= ev["tp3"] and not ev.get("runner_active"):
                        ev["runner_active"] = True
                        send_telegram(f"🔥 <b>[EVENT] TP3 REACHED</b> -> Partials booked. 4H Trail active!")
                    if ev.get("runner_active") and s4.get("last_high") and s4["last_high"] < ev["sl"]:
                        ev["sl"] = s4["last_high"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"🛡 <b>[EVENT 4H TRAIL]</b> SL lowered to ${ev['sl']:,.2f}")
                    if p >= ev["sl"]:
                        res = "4H TRAIL EXIT" if ev.get("runner_active") else ("BE EXIT" if ev["be"] else "EVENT SL HIT")
                        pnl = 4.5 if ev.get("runner_active") else (0.0 if ev["be"] else -1.0)
                        db_upsert_trade(ev, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[EVENT FINISHED] {res}</b> @ ${p:,.2f} ({pnl:+.1f}R)")
                        GLOBAL_ACTIVE["EVENT"] = None

    def scan_market(self, candles_map, p, funding_rate, oi_delta):
        with LOCK:
            c15 = candles_map.get("15m", [])
            c5 = candles_map.get("5m", [])
            c1h = candles_map.get("1h", [])
            c4 = candles_map.get("4h", [])
            if min(len(c15), len(c5), len(c1h), len(c4)) < 30: return
            
            s15 = smc_structure(c15); s5 = smc_structure(c5)
            s1h = smc_structure(c1h); s4 = smc_structure(c4)
            a15 = atr(c15); a1h = atr(c1h); a4 = atr(c4)
            vr15 = volume_ratio(c15); rsi15 = calculate_rsi(c15)
            disp15 = abs(c15[-1]["close"] - c15[-1]["open"]) > (1.1 * a15)

            # ---------------- 1. EVENT SCAN (48H SWEEP) ----------------
            sw48 = liquidity_sweep(c15, 192)
            cur = c15[-1]
            if sw48 and GLOBAL_ACTIVE["EVENT"] is None and time.time() >= self.event_lock_until:
                cand_dir = sw48["dir"]
                score, reasons = evaluate_score(cand_dir, {
                    "sweep": cand_dir, "htf_bias": s4["bias"],
                    "structure": s15["choch"] or s15["bos"], "fvg": cand_dir,
                    "displacement": disp15, "volume": vr15, "oi_change": oi_delta,
                    "funding_ok": True, "rsi_ok": True
                })
                if score >= EVENT_MIN:
                    sl = (cur["low"] - 1.5 * a15) if cand_dir == "LONG" else (cur["high"] + 1.5 * a15)
                    risk = abs(cur["close"] - sl)
                    evt = {
                        "id": f"EVT_{int(time.time()*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                        "type": "EVENT", "tf": "48H/HTF", "dir": cand_dir, "setup": "48H Sweep + MTF Confluence",
                        "score": score, "entry": cur["close"], "sl": sl,
                        "tp1": cur["close"] + risk if cand_dir == "LONG" else cur["close"] - risk,
                        "tp2": cur["close"] + (2 * risk) if cand_dir == "LONG" else cur["close"] - (2 * risk),
                        "tp3": cur["close"] + (5 * risk) if cand_dir == "LONG" else cur["close"] - (5 * risk),
                        "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False, "reasons": reasons
                    }
                    self.event_lock_until = time.time() + EVENT_COOLDOWN
                    GLOBAL_ACTIVE["EVENT"] = evt
                    db_upsert_trade(evt, status="OPEN")
                    send_telegram(
                        f"⚡ <b>BTCUSDT RARE EVENT SIGNAL</b>\n\n<b>Direction:</b> {cand_dir}\n"
                        f"🔹 <b>Entry:</b> ${cur['close']:,.2f}\n🛑 <b>SL:</b> ${sl:,.2f}\n"
                        f"🎯 <b>TP1:</b> ${evt['tp1']:,.2f}\n🎯 <b>TP2:</b> ${evt['tp2']:,.2f}\n🔥 <b>TP3:</b> ${evt['tp3']:,.2f}\n\n"
                        f"📊 <b>Score:</b> {score}/105\n💡 <b>Confluence:</b> {', '.join(reasons)}"
                    )

            # ---------------- 2. 15M NORMAL SCAN ----------------
            if GLOBAL_ACTIVE["15M"] is None and self.last_signal_time["15M"] != cur["time"]:
                sw15 = liquidity_sweep(c15, 6)
                fvg15 = detect_institutional_fvg(c15)
                cand_dir, setup_name = None, ""
                if sw15: cand_dir, setup_name = sw15["dir"], "15M Liquidity Sweep + Shift"
                elif s15["choch"]: cand_dir, setup_name = s15["choch"], "15M CHoCH Market Structure Shift"
                elif fvg15: cand_dir, setup_name = fvg15["dir"], "Institutional FVG Imbalance Fill"

                if cand_dir:
                    funding_ok = (cand_dir == "LONG" and funding_rate <= 0.0001) or (cand_dir == "SHORT" and funding_rate >= -0.0001)
                    rsi_ok = (cand_dir == "LONG" and 35 <= rsi15 <= 60) or (cand_dir == "SHORT" and 40 <= rsi15 <= 65)
                    struct_dir = s15["choch"] or s15["bos"] or s5["bos"]
                    score, reasons = evaluate_score(cand_dir, {
                        "sweep": sw15["dir"] if sw15 else None, "htf_bias": s1h["bias"],
                        "structure": struct_dir, "fvg": fvg15["dir"] if fvg15 else None,
                        "displacement": disp15, "volume": vr15, "oi_change": oi_delta,
                        "funding_ok": funding_ok, "rsi_ok": rsi_ok
                    })
                    if NORMAL_MIN <= score <= NORMAL_MAX:
                        sl = (cur["low"] - 1.2 * a15) if cand_dir == "LONG" else (cur["high"] + 1.2 * a15)
                        risk = abs(cur["close"] - sl)
                        t = {
                            "id": f"NRM_15M_{int(time.time()*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "type": "NORMAL", "tf": "15M", "dir": cand_dir, "setup": setup_name,
                            "score": score, "entry": cur["close"], "sl": sl,
                            "tp1": cur["close"] + risk if cand_dir == "LONG" else cur["close"] - risk,
                            "tp2": cur["close"] + (2 * risk) if cand_dir == "LONG" else cur["close"] - (2 * risk),
                            "tp3": cur["close"] + (3 * risk) if cand_dir == "LONG" else cur["close"] - (3 * risk),
                            "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False, "reasons": reasons
                        }
                        self.last_signal_time["15M"] = cur["time"]
                        GLOBAL_ACTIVE["15M"] = t
                        db_upsert_trade(t, status="OPEN")
                        emoji = "🟢" if cand_dir == "LONG" else "🔴"
                        send_telegram(
                            f"{emoji} <b>BTCUSDT SMC ENTRY SIGNAL [15M]</b>\n\n<b>Direction:</b> {cand_dir}\n<b>Setup:</b> {setup_name}\n\n"
                            f"🔹 <b>Entry:</b> ${cur['close']:,.2f}\n🛑 <b>SL:</b> ${sl:,.2f}\n"
                            f"🎯 <b>TP1:</b> ${t['tp1']:,.2f}\n🎯 <b>TP2:</b> ${t['tp2']:,.2f}\n🎯 <b>TP3:</b> ${t['tp3']:,.2f}\n\n"
                            f"📊 <b>Score:</b> {score}/105\n💡 <b>Confluence:</b> {', '.join(reasons)}"
                        )

            # ---------------- 3. 1H NORMAL SCAN (BOS + VOLUME PROFILE POC) ----------------
            cur1h = c1h[-1]
            if GLOBAL_ACTIVE["1H"] is None and self.last_signal_time["1H"] != cur1h["time"]:
                poc = volume_profile_poc(c1h, 60)
                d1h = None
                if cur1h["low"] <= poc and cur1h["close"] > poc and s1h["bos"] == "LONG": d1h = "LONG"
                elif cur1h["high"] >= poc and cur1h["close"] < poc and s1h["bos"] == "SHORT": d1h = "SHORT"

                if d1h and s5["bos"] == d1h:
                    score, reasons = evaluate_score(d1h, {
                        "sweep": None, "htf_bias": s4["bias"], "structure": s1h["bos"],
                        "fvg": None, "displacement": True, "volume": volume_ratio(c1h),
                        "oi_change": oi_delta, "funding_ok": True, "rsi_ok": True
                    })
                    if NORMAL_MIN <= score <= NORMAL_MAX:
                        sl = (cur1h["low"] - 1.2 * a1h) if d1h == "LONG" else (cur1h["high"] + 1.2 * a1h)
                        risk = abs(cur1h["close"] - sl)
                        t = {
                            "id": f"NRM_1H_{int(time.time()*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "type": "NORMAL", "tf": "1H", "dir": d1h, "setup": "1H BOS + Volume Profile POC",
                            "score": score, "entry": cur1h["close"], "sl": sl,
                            "tp1": cur1h["close"] + risk if d1h == "LONG" else cur1h["close"] - risk,
                            "tp2": cur1h["close"] + (2 * risk) if d1h == "LONG" else cur1h["close"] - (2 * risk),
                            "tp3": cur1h["close"] + (3 * risk) if d1h == "LONG" else cur1h["close"] - (3 * risk),
                            "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False, "reasons": reasons
                        }
                        self.last_signal_time["1H"] = cur1h["time"]
                        GLOBAL_ACTIVE["1H"] = t
                        db_upsert_trade(t, status="OPEN")
                        emoji = "🟢" if d1h == "LONG" else "🔴"
                        send_telegram(
                            f"{emoji} <b>BTCUSDT SMC ENTRY SIGNAL [1H]</b>\n\n<b>Direction:</b> {d1h}\n<b>Setup:</b> 1H BOS + VP POC\n\n"
                            f"🔹 <b>Entry:</b> ${cur1h['close']:,.2f}\n🛑 <b>SL:</b> ${sl:,.2f}\n"
                            f"🎯 <b>TP1:</b> ${t['tp1']:,.2f}\n🎯 <b>TP2:</b> ${t['tp2']:,.2f}\n🎯 <b>TP3:</b> ${t['tp3']:,.2f}\n\n"
                            f"📊 <b>Score:</b> {score}/105\n💡 <b>Confluence:</b> {', '.join(reasons)}"
                        )

            # ---------------- 4. 4H NORMAL SCAN (CHANNEL BREAK + FIB POCKET) ----------------
            cur4h = c4[-1]
            if GLOBAL_ACTIVE["4H"] is None and self.last_signal_time["4H"] != cur4h["time"]:
                ch = channel_break(c4, 30)
                fib = fib_pocket(c4, 80)
                if ch and fib and ch == fib["dir"] and s5["bos"] == ch:
                    d4h = ch
                    score, reasons = evaluate_score(d4h, {
                        "sweep": None, "htf_bias": s4["bias"], "structure": s4["bos"],
                        "fvg": None, "displacement": True, "volume": volume_ratio(c4),
                        "oi_change": oi_delta, "funding_ok": True, "rsi_ok": True
                    })
                    if NORMAL_MIN <= score <= NORMAL_MAX:
                        sl = (fib["sl_ref"] - 1.2 * a4) if d4h == "LONG" else (fib["sl_ref"] + 1.2 * a4)
                        risk = abs(cur4h["close"] - sl)
                        t = {
                            "id": f"NRM_4H_{int(time.time()*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "type": "NORMAL", "tf": "4H", "dir": d4h, "setup": "4H Channel Break + Fib Golden Pocket",
                            "score": score, "entry": cur4h["close"], "sl": sl,
                            "tp1": cur4h["close"] + risk if d4h == "LONG" else cur4h["close"] - risk,
                            "tp2": cur4h["close"] + (2 * risk) if d4h == "LONG" else cur4h["close"] - (2 * risk),
                            "tp3": cur4h["close"] + (3 * risk) if d4h == "LONG" else cur4h["close"] - (3 * risk),
                            "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False, "reasons": reasons
                        }
                        self.last_signal_time["4H"] = cur4h["time"]
                        GLOBAL_ACTIVE["4H"] = t
                        db_upsert_trade(t, status="OPEN")
                        emoji = "🟢" if d4h == "LONG" else "🔴"
                        send_telegram(
                            f"{emoji} <b>BTCUSDT SMC ENTRY SIGNAL [4H]</b>\n\n<b>Direction:</b> {d4h}\n<b>Setup:</b> Channel Break + Fib Pocket\n\n"
                            f"🔹 <b>Entry:</b> ${cur4h['close']:,.2f}\n🛑 <b>SL:</b> ${sl:,.2f}\n"
                            f"🎯 <b>TP1:</b> ${t['tp1']:,.2f}\n🎯 <b>TP2:</b> ${t['tp2']:,.2f}\n🎯 <b>TP3:</b> ${t['tp3']:,.2f}\n\n"
                            f"📊 <b>Score:</b> {score}/105\n💡 <b>Confluence:</b> {', '.join(reasons)}"
                        )

# ---------------- 7. BACKGROUND SCANNER THREAD ----------------
def run_worker_thread():
    time.sleep(2)
    logging.info("🚀 Production SMC V2 Worker Initialized...")
    send_telegram("🚀 <b>BTCUSDT Institutional V2 Worker Live</b>\nDual-Stream Multi-TF Engine Active 24/7.")

    engine = AutonomousEngine()
    candles_map = {}
    last_pull = 0; last_meta_pull = 0
    funding = 0.0; oi_delta = 0.0

    while True:
        try:
            p = fetch_price()
            now = time.time()
            if now - last_pull >= 15:
                for tf in ["5m", "15m", "1h", "4h", "1d"]:
                    k = fetch_klines(tf, 250)
                    if k: candles_map[tf] = k
                last_pull = now

            if now - last_meta_pull >= 45:
                funding = fetch_funding_rate()
                oi_delta = fetch_oi_hist()
                last_meta_pull = now

            if p > 10000:
                ENGINE_STATUS["last_price"] = p
                ENGINE_STATUS["last_scan_time"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
                ENGINE_STATUS["scans_completed"] += 1

                c4 = candles_map.get("4h", [])
                s4 = smc_structure(c4) if len(c4) > 20 else {}
                engine.manage_positions(p, s4)
                engine.scan_market(candles_map, p, funding, oi_delta)

            time.sleep(3)
        except Exception as e:
            logging.error(f"Worker Engine Exception: {e}\n{traceback.format_exc()}")
            time.sleep(5)

threading.Thread(target=run_worker_thread, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
