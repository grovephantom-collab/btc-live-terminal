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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [MASTER-WORKER] %(message)s"
)

app = Flask(__name__)

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
BASE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

NORMAL_MIN = 70
NORMAL_MAX = 84
EVENT_MIN = 85
EVENT_COOLDOWN = 12 * 60 * 60

LOCK = threading.RLock()

DATA_HUB = {
    "price": 0.0,
    "funding": 0.0,
    "oi_delta": 0.0,
    "klines": {
        "1m": [], "5m": [], "15m": [], "1h": [], "4h": [], "1d": []
    },
    "last_sync": 0.0
}

ACTIVE_POI_REGISTRY = []

ENGINE_STATUS = {
    "boot_time": datetime.now(timezone.utc).isoformat(),
    "last_scan_time": None,
    "last_price": 0.0,
    "worker_alive": True,
    "scans_completed": 0
}

GLOBAL_ACTIVE = {
    "NORMAL": None,
    "EVENT": None
}

# ---------------- 1. SQLITE WAL DB ----------------
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
        exit REAL, result TEXT, pnl_r REAL, realized_r REAL, remaining_pct REAL, stage TEXT,
        confluence TEXT, status TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS event_locks(
        event_key TEXT PRIMARY KEY, created_at REAL, direction TEXT,
        score INTEGER, status TEXT)""")
    c.commit()
    c.close()

init_db()

# ---------------- 2. REST API ----------------
@app.route("/")
@app.route("/healthz")
def health():
    return "BTCUSDT SMC Master V7 Engine Online", 200

@app.route("/status")
def api_status():
    return jsonify({
        "status": "healthy" if ENGINE_STATUS["worker_alive"] else "degraded",
        "last_scan": ENGINE_STATUS["last_scan_time"],
        "last_price": ENGINE_STATUS["last_price"],
        "scans": ENGINE_STATUS["scans_completed"],
        "boot_time": ENGINE_STATUS["boot_time"]
    }), 200

@app.route("/price")
def api_price():
    return jsonify({"symbol": SYMBOL, "price": DATA_HUB["price"]}), 200

@app.route("/poi-zones")
def api_poi_zones():
    with LOCK:
        return jsonify(ACTIVE_POI_REGISTRY), 200

@app.route("/events")
def api_events():
    with LOCK:
        return jsonify({
            "event_active": GLOBAL_ACTIVE["EVENT"] is not None,
            "event": GLOBAL_ACTIVE["EVENT"]
        }), 200

@app.route("/active-trades")
def api_active():
    with LOCK:
        active_list = []
        for k, t in GLOBAL_ACTIVE.items():
            if t:
                active_list.append({
                    "id": t["id"], "type": t["type"], "tf": t["tf"], "dir": t["dir"],
                    "entry": float(t["entry"]), "sl": float(t["sl"]),
                    "tp1": float(t["tp1"]), "tp2": float(t["tp2"]), "tp3": float(t["tp3"]),
                    "setup": t["setup"], "score": int(t["score"]), "stage": t.get("stage", "OPEN")
                })

        try:
            c = db()
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM trades WHERE signal_type='SNIPER' AND status='OPEN'").fetchall()
            c.close()
            for r in rows:
                active_list.append({
                    "id": r["trade_id"], "type": "SNIPER", "tf": r["tf"], "dir": r["direction"],
                    "entry": float(r["entry"]), "sl": float(r["sl"]),
                    "tp1": float(r["tp1"]), "tp2": float(r["tp2"]), "tp3": float(r["tp3"]),
                    "setup": r["setup"], "score": int(r["score"]), "stage": r["stage"]
                })
        except Exception:
            pass

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

        active_res = api_active()[0].get_json()
        with LOCK:
            pois = list(ACTIVE_POI_REGISTRY)

        return jsonify({
            "vault": formatted_vault,
            "active": active_res,
            "pois": pois
        }), 200
    except Exception as e:
        return jsonify({"vault": [], "active": [], "pois": []}), 500

# ---------------- 3. TELEGRAM CLIENT ----------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logging.error(f"Telegram dispatch failed: {e}")

def db_upsert_trade(t, status="OPEN", exit_p=0.0, res="RUNNING", final_pnl=0.0):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, realized_r, remaining_pct, stage, confluence, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], t["score"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], exit_p, res, final_pnl,
                   t.get("realized_r", 0.0), t.get("remaining_pct", 1.0), t.get("stage", "OPEN"),
                   ", ".join(t.get("reasons", [])), status))
        c.commit(); c.close()
    except Exception as e:
        logging.error(f"DB Error: {e}")

# ---------------- 4. CENTRAL DATA HUB FETCHER ----------------
def fetch_klines(interval, limit=250):
    try:
        r = requests.get(f"{BASE}/fapi/v1/klines", params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=4).json()
        if isinstance(r, list) and len(r) > 10:
            return [{
                "time": int(b[0] // 1000), "open": float(b[1]), "high": float(b[2]),
                "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])
            } for b in r[:-1]]
    except Exception:
        pass
    return []

def sync_central_data_hub():
    global DATA_HUB
    try:
        p_res = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["price"] = float(p_res.get("price", 0.0))

        fr_res = requests.get(f"{BASE}/fapi/v1/premiumIndex", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["funding"] = float(fr_res.get("lastFundingRate", 0.0))

        oi_raw = requests.get(f"{BASE}/futures/data/openInterestHist", params={"symbol": SYMBOL, "period": "15m", "limit": 4}, timeout=3).json()
        if isinstance(oi_raw, list) and len(oi_raw) >= 2:
            cur_oi = float(oi_raw[-1]["sumOpenInterest"])
            prev_oi = float(oi_raw[-2]["sumOpenInterest"])
            DATA_HUB["oi_delta"] = ((cur_oi - prev_oi) / prev_oi) if prev_oi > 0 else 0.0

        for tf in ["1m", "5m", "15m", "1h", "4h", "1d"]:
            k = fetch_klines(tf, 250)
            if k: DATA_HUB["klines"][tf] = k

        DATA_HUB["last_sync"] = time.time()
    except Exception as e:
        logging.warning(f"Hub sync warning: {e}")

# ---------------- 5. SMC STRUCTURE & LIQUIDITY ----------------
def atr(candles, period=14):
    if len(candles) < period + 1: return 120.0
    trs = [max(candles[i]["high"] - candles[i]["low"], 
               abs(candles[i]["high"] - candles[i-1]["close"]), 
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

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
    cur = candles[-1]; close = cur["close"]
    body = abs(cur["close"] - cur["open"]); a = atr(candles)

    bias = "NEUTRAL"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]: bias = "BULL"
        elif highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]: bias = "BEAR"

    bos, choch = None, None
    has_disp = body >= (0.8 * a)
    if bias == "BULL":
        if close > last_h: bos = "LONG"
        if close < (lows[-2][1] if len(lows) >= 2 else last_l) and has_disp: choch = "SHORT"
    elif bias == "BEAR":
        if close < last_l: bos = "SHORT"
        if close > (highs[-2][1] if len(highs) >= 2 else last_h) and has_disp: choch = "LONG"

    return {"bias": bias, "bos": bos, "choch": choch, "last_high": last_h, "last_low": last_l}

def liquidity_sweep(candles, lookback=10):
    if len(candles) < lookback + 2: return None
    ref = candles[-lookback-1:-1]; cur = candles[-1]
    hi, lo = max(x["high"] for x in ref), min(x["low"] for x in ref)
    if cur["low"] < lo and cur["close"] > lo: return {"dir": "LONG", "level": lo}
    if cur["high"] > hi and cur["close"] < hi: return {"dir": "SHORT", "level": hi}
    return None

def detect_unmitigated_fvg(candles, lookback=20):
    if len(candles) < 5: return None
    for i in range(len(candles) - 3, max(0, len(candles) - lookback), -1):
        c1, _, c3 = candles[i-2], candles[i-1], candles[i]
        if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) > 15.0:
            top, bot = c3["low"], c1["high"]
            if not any(candles[j]["low"] < bot for j in range(i+1, len(candles))):
                return {"dir": "LONG", "low": bot, "high": top}
        if c3["high"] < c1["low"] and (c1["low"] - c3["high"]) > 15.0:
            bot, top = c3["high"], c1["low"]
            if not any(candles[j]["high"] > top for j in range(i+1, len(candles))):
                return {"dir": "SHORT", "low": bot, "high": top}
    return None

def check_5m_confirmation(c5, direction):
    if len(c5) < 15: return False
    s5 = smc_structure(c5)
    cur5 = c5[-1]
    a5 = atr(c5)
    disp5 = abs(cur5["close"] - cur5["open"]) >= (0.85 * a5)
    if direction == "LONG":
        return (s5["bos"] == "LONG" or s5["choch"] == "LONG") and (disp5 or cur5["close"] > cur5["open"])
    elif direction == "SHORT":
        return (s5["bos"] == "SHORT" or s5["choch"] == "SHORT") and (disp5 or cur5["close"] < cur5["open"])
    return False

# ---------------- 6. TRADE MANAGER ----------------
class TradeManager:
    def __init__(self):
        self.active_event_key = None
        self.event_cooldown_until = 0.0

    def manage_positions(self, p, s4, a4):
        with LOCK:
            # 1. NORMAL TRADE MANAGEMENT
            t = GLOBAL_ACTIVE["NORMAL"]
            if t:
                if t["dir"] == "LONG":
                    if p >= t["tp1"] and t["stage"] == "OPEN":
                        t["stage"] = "TP1_DONE"
                        t["realized_r"] += 0.333 * 1.0
                        t["remaining_pct"] = 0.667
                        t["sl"] = t["entry"]
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[NORMAL] TP1 (+1R) REACHED</b>\n33.3% banked. 🛡 SL moved to BE (${t['entry']:,.2f})")

                    elif p >= t["tp2"] and t["stage"] == "TP1_DONE":
                        t["stage"] = "TP2_DONE"
                        t["realized_r"] += 0.333 * 2.0
                        t["remaining_pct"] = 0.334
                        t["sl"] = t["tp1"]
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[NORMAL] TP2 (+2R) REACHED</b>\nNext 33.3% banked. 🛡 SL trailed to TP1 (${t['tp1']:,.2f})")

                    if p <= t["sl"]:
                        if t["stage"] == "OPEN": final_r = -1.0; res = "SL HIT"
                        elif t["stage"] == "TP1_DONE": final_r = t["realized_r"]; res = "BE EXIT"
                        else: final_r = t["realized_r"] + (t["remaining_pct"] * 1.0); res = "TP1 TRAIL EXIT"
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                        send_telegram(f"🏁 <b>[NORMAL] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.3f}R</b>")
                        GLOBAL_ACTIVE["NORMAL"] = None

                    elif p >= t["tp3"] and t["stage"] in ["OPEN", "TP1_DONE", "TP2_DONE"]:
                        final_r = t["realized_r"] + (t["remaining_pct"] * 3.0)
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", final_pnl=round(final_r, 3))
                        send_telegram(f"🔥 <b>[NORMAL] FULL TP3 REACHED!</b> @ ${p:,.2f} | Net: <b>+{final_r:.3f}R</b>")
                        GLOBAL_ACTIVE["NORMAL"] = None

                elif t["dir"] == "SHORT":
                    if p <= t["tp1"] and t["stage"] == "OPEN":
                        t["stage"] = "TP1_DONE"
                        t["realized_r"] += 0.333 * 1.0
                        t["remaining_pct"] = 0.667
                        t["sl"] = t["entry"]
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[NORMAL] TP1 (+1R) REACHED</b>\n33.3% banked. 🛡 SL moved to BE (${t['entry']:,.2f})")

                    elif p <= t["tp2"] and t["stage"] == "TP1_DONE":
                        t["stage"] = "TP2_DONE"
                        t["realized_r"] += 0.333 * 2.0
                        t["remaining_pct"] = 0.334
                        t["sl"] = t["tp1"]
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[NORMAL] TP2 (+2R) REACHED</b>\nNext 33.3% banked. 🛡 SL trailed to TP1 (${t['tp1']:,.2f})")

                    if p >= t["sl"]:
                        if t["stage"] == "OPEN": final_r = -1.0; res = "SL HIT"
                        elif t["stage"] == "TP1_DONE": final_r = t["realized_r"]; res = "BE EXIT"
                        else: final_r = t["realized_r"] + (t["remaining_pct"] * 1.0); res = "TP1 TRAIL EXIT"
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                        send_telegram(f"🏁 <b>[NORMAL] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.3f}R</b>")
                        GLOBAL_ACTIVE["NORMAL"] = None

                    elif p <= t["tp3"] and t["stage"] in ["OPEN", "TP1_DONE", "TP2_DONE"]:
                        final_r = t["realized_r"] + (t["remaining_pct"] * 3.0)
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", final_pnl=round(final_r, 3))
                        send_telegram(f"🔥 <b>[NORMAL] FULL TP3 REACHED!</b> @ ${p:,.2f} | Net: <b>+{final_r:.3f}R</b>")
                        GLOBAL_ACTIVE["NORMAL"] = None

            # 2. 48H EVENT MANAGEMENT (25% Partials + 4H Trailing Runner)
            ev = GLOBAL_ACTIVE["EVENT"]
            if ev:
                risk = abs(ev["entry"] - ev["sl"]) if abs(ev["entry"] - ev["sl"]) > 0 else 1.0
                if ev["dir"] == "LONG":
                    if p >= ev["tp1"] and ev["stage"] == "OPEN":
                        ev["stage"] = "TP1_DONE"
                        ev["realized_r"] += 0.25 * 1.0
                        ev["remaining_pct"] = 0.75
                        ev["sl"] = ev["entry"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP1 (+1R)</b> -> 25% banked. SL locked to BE (${ev['entry']:,.2f})")

                    elif p >= ev["tp2"] and ev["stage"] == "TP1_DONE":
                        ev["stage"] = "TP2_DONE"
                        ev["realized_r"] += 0.25 * 2.0
                        ev["remaining_pct"] = 0.50
                        ev["sl"] = ev["tp1"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP2 (+2R)</b> -> 25% banked. SL locked to TP1 (${ev['tp1']:,.2f})")

                    elif p >= ev["tp3"] and ev["stage"] == "TP2_DONE":
                        ev["stage"] = "RUNNER_ACTIVE"
                        ev["realized_r"] += 0.25 * 4.0
                        ev["remaining_pct"] = 0.25
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"🔥 <b>[EVENT] TP3 (+4R) REACHED</b> -> 75% banked. 25% Runner active!")

                    if ev["stage"] == "RUNNER_ACTIVE" and s4.get("last_low"):
                        trail_target = s4["last_low"] - (0.5 * a4)
                        if trail_target > ev["sl"]:
                            ev["sl"] = trail_target
                            db_upsert_trade(ev, status="OPEN")

                    if p <= ev["sl"]:
                        if ev["stage"] == "OPEN": final_r = -1.0; res = "EVENT SL HIT"
                        elif ev["stage"] == "TP1_DONE": final_r = ev["realized_r"]; res = "BE EXIT"
                        elif ev["stage"] == "TP2_DONE": final_r = ev["realized_r"] + (ev["remaining_pct"] * 1.0); res = "TP1 LOCK EXIT"
                        else:
                            runner_r = (p - ev["entry"]) / risk
                            final_r = ev["realized_r"] + (ev["remaining_pct"] * runner_r)
                            res = "4H TRAIL EXIT"
                        db_upsert_trade(ev, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                        send_telegram(f"🏁 <b>[EVENT COMPLETED] {res}</b> @ ${p:,.2f} | Final: <b>{final_r:+.3f}R</b>")
                        GLOBAL_ACTIVE["EVENT"] = None

                elif ev["dir"] == "SHORT":
                    if p <= ev["tp1"] and ev["stage"] == "OPEN":
                        ev["stage"] = "TP1_DONE"
                        ev["realized_r"] += 0.25 * 1.0
                        ev["remaining_pct"] = 0.75
                        ev["sl"] = ev["entry"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP1 (+1R)</b> -> 25% banked. SL locked to BE (${ev['entry']:,.2f})")

                    elif p <= ev["tp2"] and ev["stage"] == "TP1_DONE":
                        ev["stage"] = "TP2_DONE"
                        ev["realized_r"] += 0.25 * 2.0
                        ev["remaining_pct"] = 0.50
                        ev["sl"] = ev["tp1"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP2 (+2R)</b> -> 25% banked. SL locked to TP1 (${ev['tp1']:,.2f})")

                    elif p <= ev["tp3"] and ev["stage"] == "TP2_DONE":
                        ev["stage"] = "RUNNER_ACTIVE"
                        ev["realized_r"] += 0.25 * 4.0
                        ev["remaining_pct"] = 0.25
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"🔥 <b>[EVENT] TP3 (+4R) REACHED</b> -> 75% banked. 25% Runner active!")

                    if ev["stage"] == "RUNNER_ACTIVE" and s4.get("last_high"):
                        trail_target = s4["last_high"] + (0.5 * a4)
                        if trail_target < ev["sl"]:
                            ev["sl"] = trail_target
                            db_upsert_trade(ev, status="OPEN")

                    if p >= ev["sl"]:
                        if ev["stage"] == "OPEN": final_r = -1.0; res = "EVENT SL HIT"
                        elif ev["stage"] == "TP1_DONE": final_r = ev["realized_r"]; res = "BE EXIT"
                        elif ev["stage"] == "TP2_DONE": final_r = ev["realized_r"] + (ev["remaining_pct"] * 1.0); res = "TP1 LOCK EXIT"
                        else:
                            runner_r = (ev["entry"] - p) / risk
                            final_r = ev["realized_r"] + (ev["remaining_pct"] * runner_r)
                            res = "4H TRAIL EXIT"
                        db_upsert_trade(ev, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                        send_telegram(f"🏁 <b>[EVENT COMPLETED] {res}</b> @ ${p:,.2f} | Final: <b>{final_r:+.3f}R</b>")
                        GLOBAL_ACTIVE["EVENT"] = None

    def scan_master_and_event(self):
        global ACTIVE_POI_REGISTRY
        with LOCK:
            hub = DATA_HUB
            c15 = hub["klines"].get("15m", [])
            c5 = hub["klines"].get("5m", [])
            c1h = hub["klines"].get("1h", [])
            c4 = hub["klines"].get("4h", [])
            cd = hub["klines"].get("1d", [])

            if min(len(c15), len(c5), len(c1h), len(c4)) < 25: return

            s15 = smc_structure(c15); s1h = smc_structure(c1h); s4 = smc_structure(c4)
            sd = smc_structure(cd) if len(cd) > 20 else {}
            cur = c15[-1]
            a15 = atr(c15); a4 = atr(c4)

            # Expose Active POIs to Sniper
            fvg15 = detect_unmitigated_fvg(c15)
            fvg1h = detect_unmitigated_fvg(c1h)
            new_pois = []
            if fvg15: new_pois.append({"type": "15M_FVG", "dir": fvg15["dir"], "low": fvg15["low"], "high": fvg15["high"]})
            if fvg1h: new_pois.append({"type": "1H_FVG", "dir": fvg1h["dir"], "low": fvg1h["low"], "high": fvg1h["high"]})
            ACTIVE_POI_REGISTRY = new_pois

            # 48H EVENT SCAN (Score 85+)
            sw48 = liquidity_sweep(c15, 192)
            if sw48 and GLOBAL_ACTIVE["EVENT"] is None:
                cand_dir = sw48["dir"]
                pivot_ref = s4.get("last_low") if cand_dir == "LONG" else s4.get("last_high")
                event_key = f"{cand_dir}_{int(sw48['level']//50)*50}_{int((pivot_ref or 0)//50)*50}"

                now = time.time()
                if event_key != self.active_event_key or now >= self.event_cooldown_until:
                    desired_bias = "BULL" if cand_dir == "LONG" else "BEAR"
                    if s4.get("bias") == desired_bias and s1h.get("bias") == desired_bias and check_5m_confirmation(c5, cand_dir):
                        sl = (cur["low"] - 1.5 * a15) if cand_dir == "LONG" else (cur["high"] + 1.5 * a15)
                        risk = abs(cur["close"] - sl)
                        evt = {
                            "id": f"EVT_{int(now*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "type": "EVENT", "tf": "48H/HTF", "dir": cand_dir, "setup": "48H Sweep + Multi-TF Alignment",
                            "score": 90, "entry": cur["close"], "sl": sl,
                            "tp1": cur["close"] + risk if cand_dir == "LONG" else cur["close"] - risk,
                            "tp2": cur["close"] + (2 * risk) if cand_dir == "LONG" else cur["close"] - (2 * risk),
                            "tp3": cur["close"] + (4 * risk) if cand_dir == "LONG" else cur["close"] - (4 * risk),
                            "realized_r": 0.0, "remaining_pct": 1.0, "stage": "OPEN", "reasons": ["48H Sweep", "HTF Alignment"]
                        }
                        self.active_event_key = event_key
                        self.event_cooldown_until = now + EVENT_COOLDOWN
                        GLOBAL_ACTIVE["EVENT"] = evt
                        db_upsert_trade(evt, status="OPEN")
                        send_telegram(
                            f"🔥 <b>[EVENT SIGNAL]</b>\n\n<b>{cand_dir}</b> @ ${cur['close']:,.2f}\n"
                            f"SL: ${sl:,.2f} | TP1: ${evt['tp1']:,.2f} | TP2: ${evt['tp2']:,.2f}\nScore: 90/105"
                        )

def run_worker_daemon():
    time.sleep(2)
    logging.info("🚀 Production Worker Daemon Live...")
    send_telegram("🚀 <b>BTCUSDT Master Engine & Hub Online 24/7</b>")

    manager = TradeManager()

    while True:
        try:
            sync_central_data_hub()
            p = DATA_HUB["price"]

            if p > 10000:
                ENGINE_STATUS["last_price"] = p
                ENGINE_STATUS["last_scan_time"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
                ENGINE_STATUS["scans_completed"] += 1

                c4 = DATA_HUB["klines"].get("4h", [])
                s4 = smc_structure(c4) if len(c4) > 20 else {}
                a4 = atr(c4) if len(c4) > 20 else 120.0

                manager.manage_positions(p, s4, a4)
                manager.scan_master_and_event()

            time.sleep(3)
        except Exception as e:
            logging.error(f"Worker daemon error: {e}\n{traceback.format_exc()}")
            time.sleep(4)

threading.Thread(target=run_worker_daemon, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
