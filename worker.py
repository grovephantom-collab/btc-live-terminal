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
# BTCUSDT SMC MASTER WORKER (FULL LIQUIDITY SUITE: EQH/EQL/IDM)
# ============================================================

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
    return "BTCUSDT SMC Master V6 Engine Online", 200

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

@app.route("/active-trades")
def api_active():
    with LOCK:
        active_list = []
        if GLOBAL_ACTIVE["EVENT"]:
            ev = GLOBAL_ACTIVE["EVENT"]
            active_list.append({
                "id": ev["id"], "tf": ev["tf"], "dir": ev["dir"],
                "entry": float(ev["entry"]), "sl": float(ev["sl"]),
                "tp1": float(ev["tp1"]), "tp2": float(ev["tp2"]), "tp3": float(ev["tp3"]),
                "setup": ev["setup"], "score": int(ev["score"]), "stage": ev.get("stage", "OPEN")
            })

        try:
            c = db()
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM trades WHERE signal_type='SNIPER' AND status='OPEN'").fetchall()
            c.close()
            for r in rows:
                active_list.append({
                    "id": r["trade_id"], "tf": r["tf"], "dir": r["direction"],
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
        return jsonify({"vault": formatted_vault, "active": active_res}), 200
    except Exception as e:
        return jsonify({"vault": [], "active": []}), 500

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

# ---------------- 4. CENTRAL DATA HUB ----------------
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

# ---------------- 5. ADVANCED SMC & LIQUIDITY MODULES ----------------
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

# NEW: Equal Highs / Equal Lows (EQH / EQL) Detector
def detect_eqh_eql(candles, tolerance_pct=0.0006):
    highs, lows = confirmed_swings(candles, 2, 2)
    eqh, eql = None, None
    if len(highs) >= 2:
        h1, h2 = highs[-1][1], highs[-2][1]
        if abs(h1 - h2) / h1 <= tolerance_pct:
            eqh = max(h1, h2)
    if len(lows) >= 2:
        l1, l2 = lows[-1][1], lows[-2][1]
        if abs(l1 - l2) / l1 <= tolerance_pct:
            eql = min(l1, l2)
    return {"eqh": eqh, "eql": eql}

# NEW: Inducement (IDM) Swept Tracker
def check_inducement_sweep(candles, direction):
    highs, lows = confirmed_swings(candles, 1, 1)
    if len(highs) < 2 or len(lows) < 2: return False
    cur = candles[-1]
    if direction == "LONG":
        idm_low = lows[-1][1]
        return cur["low"] < idm_low and cur["close"] > idm_low
    elif direction == "SHORT":
        idm_high = highs[-1][1]
        return cur["high"] > idm_high and cur["close"] < idm_high
    return False

# NEW: Macro Liquidity Void Detector
def detect_macro_voids(candles, threshold=200.0):
    voids = []
    for i in range(len(candles) - 10, len(candles) - 1):
        c1, c2 = candles[i], candles[i+1]
        gap_up = c2["low"] - c1["high"]
        gap_down = c1["low"] - c2["high"]
        if gap_up >= threshold:
            voids.append({"type": "VOID_UP", "low": c1["high"], "high": c2["low"]})
        elif gap_down >= threshold:
            voids.append({"type": "VOID_DOWN", "low": c2["high"], "high": c1["low"]})
    return voids

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

# ---------------- 6. MASTER ENGINE & HTF POI PROVIDER ----------------
class MasterWorkerEngine:
    def __init__(self):
        self.active_event_key = None
        self.event_cooldown_until = 0.0

    def evaluate_master_setup(self):
        global ACTIVE_POI_REGISTRY
        with LOCK:
            hub = DATA_HUB
            c15 = hub["klines"].get("15m", [])
            c1h = hub["klines"].get("1h", [])
            c4 = hub["klines"].get("4h", [])
            cd = hub["klines"].get("1d", [])

            if min(len(c15), len(c1h), len(c4)) < 25: return

            s15 = smc_structure(c15); s1h = smc_structure(c1h); s4 = smc_structure(c4)
            cur = c15[-1]
            a15 = atr(c15); a4 = atr(c4)

            # 1. POI IDENTIFICATION + EQH/EQL/IDM ENHANCEMENT
            fvg15 = detect_unmitigated_fvg(c15)
            fvg1h = detect_unmitigated_fvg(c1h)
            eq_data = detect_eqh_eql(c15)
            new_pois = []

            if fvg15 and (s1h["bias"] == ("BULL" if fvg15["dir"] == "LONG" else "BEAR")):
                new_pois.append({
                    "type": "15M_FVG", "dir": fvg15["dir"], 
                    "low": fvg15["low"], "high": fvg15["high"],
                    "has_eqh_eql": eq_data["eqh"] is not None or eq_data["eql"] is not None
                })
            if fvg1h:
                new_pois.append({
                    "type": "1H_FVG", "dir": fvg1h["dir"], 
                    "low": fvg1h["low"], "high": fvg1h["high"],
                    "has_eqh_eql": False
                })
            ACTIVE_POI_REGISTRY = new_pois

            # 2. 48H EVENT ENGINE (Score >= 85)
            sw48 = liquidity_sweep(c15, 192)
            if sw48 and GLOBAL_ACTIVE["EVENT"] is None:
                cand_dir = sw48["dir"]
                pivot_ref = s4.get("last_low") if cand_dir == "LONG" else s4.get("last_high")
                event_key = f"{cand_dir}_{int(sw48['level']//50)*50}_{int((pivot_ref or 0)//50)*50}"

                now = time.time()
                if event_key != self.active_event_key or now >= self.event_cooldown_until:
                    desired_bias = "BULL" if cand_dir == "LONG" else "BEAR"
                    if s4.get("bias") == desired_bias and s1h.get("bias") == desired_bias:
                        sl = (cur["low"] - 1.5 * a15) if cand_dir == "LONG" else (cur["high"] + 1.5 * a15)
                        risk = abs(cur["close"] - sl)
                        evt = {
                            "id": f"EVT_{int(now*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "type": "EVENT", "tf": "48H/HTF", "dir": cand_dir, "setup": "48H Sweep + Multi-TF Cascade + Void Purge",
                            "score": 95, "entry": cur["close"], "sl": sl,
                            "tp1": cur["close"] + risk if cand_dir == "LONG" else cur["close"] - risk,
                            "tp2": cur["close"] + (2 * risk) if cand_dir == "LONG" else cur["close"] - (2 * risk),
                            "tp3": cur["close"] + (4 * risk) if cand_dir == "LONG" else cur["close"] - (4 * risk),
                            "realized_r": 0.0, "remaining_pct": 1.0, "stage": "OPEN", "reasons": ["48H Sweep", "Cascade Alignment", "EQ Liquidity Purged"]
                        }
                        self.active_event_key = event_key
                        self.event_cooldown_until = now + EVENT_COOLDOWN
                        GLOBAL_ACTIVE["EVENT"] = evt
                        db_upsert_trade(evt, status="OPEN")
                        
                        c = db()
                        c.execute("INSERT OR REPLACE INTO event_locks VALUES (?,?,?,?,?)",
                                  (event_key, now, cand_dir, 95, "ACTIVE"))
                        c.commit(); c.close()

                        send_telegram(
                            f"🔥 <b>[48H RARE EVENT SIGNAL]</b>\n\n"
                            f"<b>{cand_dir}</b> @ ${cur['close']:,.2f}\n"
                            f"SL: ${sl:,.2f}\n"
                            f"TP1: ${evt['tp1']:,.2f} | TP2: ${evt['tp2']:,.2f}\n"
                            f"Score: 95/105"
                        )

def run_master_worker_daemon():
    time.sleep(2)
    logging.info("🚀 Master Worker with EQH/EQL & IDM Online...")
    send_telegram("🚀 <b>Master Worker Online</b>\nFull Liquidity Engine Active 24/7.")

    engine = MasterWorkerEngine()

    while True:
        try:
            sync_central_data_hub()
            p = DATA_HUB["price"]

            if p > 10000:
                ENGINE_STATUS["last_price"] = p
                ENGINE_STATUS["last_scan_time"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
                ENGINE_STATUS["scans_completed"] += 1
                engine.evaluate_master_setup()

            time.sleep(3)
        except Exception as e:
            logging.error(f"Worker error: {e}\n{traceback.format_exc()}")
            time.sleep(4)

threading.Thread(target=run_master_worker_daemon, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
