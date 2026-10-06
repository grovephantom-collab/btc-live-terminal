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
    format="%(asctime)s [%(levelname)s] [UNIFIED-WORKER] %(message)s"
)

app = Flask(__name__)

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
BASE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

LOCK = threading.RLock()
STARTED_FLAG = False

DATA_HUB = {
    "price": 0.0,
    "klines": {"1m": [], "5m": [], "15m": [], "1h": [], "4h": []},
    "last_sync": 0.0
}

ACTIVE_TRADES = []
ACTIVE_POIS = []

# ---------------- 1. SQLITE SETUP ----------------
def db():
    c = sqlite3.connect(DB_FILE, timeout=20)
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
    c.commit()
    c.close()

init_db()

# ---------------- 2. TELEGRAM (NO SPAM) ----------------
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
        logging.error(f"Telegram error: {e}")

# ---------------- 3. FLASK REST API ----------------
@app.route("/")
@app.route("/healthz")
def health():
    return "BTCUSDT SMC Engine Running 24/7", 200

@app.route("/price")
def api_price():
    return jsonify({"symbol": SYMBOL, "price": DATA_HUB["price"]}), 200

@app.route("/active-trades")
def api_active():
    with LOCK:
        return jsonify(ACTIVE_TRADES), 200

@app.route("/vault-data")
def api_vault_data():
    try:
        c = db()
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        c.close()

        vault = []
        for r in rows:
            vault.append({
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

        with LOCK:
            active = list(ACTIVE_TRADES)
            pois = list(ACTIVE_POIS)

        return jsonify({"vault": vault, "active": active, "pois": pois}), 200
    except Exception as e:
        return jsonify({"vault": [], "active": [], "pois": []}), 500

# ---------------- 4. MARKET DATA FETCHER ----------------
def fetch_klines(interval, limit=120):
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

def sync_data():
    global DATA_HUB
    try:
        p_res = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["price"] = float(p_res.get("price", 0.0))

        for tf in ["1m", "5m", "15m", "1h", "4h"]:
            k = fetch_klines(tf, 100)
            if k:
                DATA_HUB["klines"][tf] = k
        DATA_HUB["last_sync"] = time.time()
    except Exception as e:
        logging.warning(f"Sync error: {e}")

# ---------------- 5. SMC CALCULATION CORE ----------------
def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 50.0
    trs = [max(candles[i]["high"] - candles[i]["low"],
               abs(candles[i]["high"] - candles[i-1]["close"]),
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

def detect_sweep(candles, lookback=8):
    if len(candles) < lookback + 2: return None
    ref = candles[-lookback-1:-1]
    cur = candles[-1]
    hi = max(c["high"] for c in ref)
    lo = min(c["low"] for c in ref)
    if cur["low"] < lo and cur["close"] > lo:
        return {"dir": "LONG", "wick": cur["low"], "level": lo}
    if cur["high"] > hi and cur["close"] < hi:
        return {"dir": "SHORT", "wick": cur["high"], "level": hi}
    return None

def detect_fvg(candles):
    if len(candles) < 4: return None
    c1, _, c3 = candles[-4], candles[-3], candles[-2]
    cur = candles[-1]
    # Bullish FVG
    if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) >= 8.0:
        ce = c1["high"] + ((c3["low"] - c1["high"]) * 0.5)
        if cur["low"] <= c3["low"]:
            return {"dir": "LONG", "low": c1["high"], "high": c3["low"], "ce": ce}
    # Bearish FVG
    if c3["high"] < c1["low"] and (c1["low"] - c3["high"]) >= 8.0:
        ce = c3["high"] + ((c1["low"] - c3["high"]) * 0.5)
        if cur["high"] >= c3["high"]:
            return {"dir": "SHORT", "low": c3["high"], "high": c1["low"], "ce": ce}
    return None

# ---------------- 6. TRADE MANAGER (REAL-TIME) ----------------
def manage_positions(p):
    global ACTIVE_TRADES
    with LOCK:
        remaining = []
        for t in ACTIVE_TRADES:
            entry = t["entry"]
            sl = t["sl"]
            risk = abs(entry - sl) if abs(entry - sl) > 0 else 1.0

            # LONG MANAGEMENT
            if t["dir"] == "LONG":
                # TP1 (+2R)
                if p >= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 2.0
                    t["sl"] = entry # Move to Breakeven
                    send_telegram(f"🎯 <b>[{t['type']}] TP1 HIT (+2R)</b>\n50% secured. 🛡 SL moved to <b>BE (${entry:,.2f})</b>")

                # TP2 (+4R)
                elif p >= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 4.0
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] TP2 HIT (+4R)</b>\n25% secured. 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")

                # SL / Invalidation
                if p <= t["sl"]:
                    final_r = -1.0 if t["stage"] == "OPEN" else t["realized_r"]
                    res = "SL HIT" if t["stage"] == "OPEN" else "BE/TRAIL EXIT"
                    save_closed_trade(t, p, res, final_r)
                    send_telegram(f"🏁 <b>[{t['type']} CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    continue

                # TP3 Full Target (+6R)
                elif p >= t["tp3"]:
                    final_r = t["realized_r"] + (0.25 * 6.0)
                    save_closed_trade(t, p, "TP3 TARGET 🔥", final_r)
                    send_telegram(f"🔥 <b>[{t['type']} FULL TARGET]</b> @ ${p:,.2f} | Net: <b>+{final_r:+.2f}R</b>")
                    continue

            # SHORT MANAGEMENT
            elif t["dir"] == "SHORT":
                if p <= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 2.0
                    t["sl"] = entry
                    send_telegram(f"🎯 <b>[{t['type']}] TP1 HIT (+2R)</b>\n50% secured. 🛡 SL moved to <b>BE (${entry:,.2f})</b>")

                elif p <= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 4.0
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] TP2 HIT (+4R)</b>\n25% secured. 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")

                if p >= t["sl"]:
                    final_r = -1.0 if t["stage"] == "OPEN" else t["realized_r"]
                    res = "SL HIT" if t["stage"] == "OPEN" else "BE/TRAIL EXIT"
                    save_closed_trade(t, p, res, final_r)
                    send_telegram(f"🏁 <b>[{t['type']} CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    continue

                elif p <= t["tp3"]:
                    final_r = t["realized_r"] + (0.25 * 6.0)
                    save_closed_trade(t, p, "TP3 TARGET 🔥", final_r)
                    send_telegram(f"🔥 <b>[{t['type']} FULL TARGET]</b> @ ${p:,.2f} | Net: <b>+{final_r:+.2f}R</b>")
                    continue

            remaining.append(t)
        ACTIVE_TRADES = remaining

def save_closed_trade(t, exit_p, res, pnl):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, realized_r, remaining_pct, stage, confluence, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], t["score"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], exit_p, res, pnl,
                   t.get("realized_r", 0.0), 0.0, "CLOSED", "", "CLOSED"))
        c.commit()
        c.close()
    except Exception as e:
        logging.error(f"DB close save error: {e}")

# ---------------- 7. SCANNER PIPELINE ----------------
LAST_SCAN_TIME = 0

def scan_signals():
    global ACTIVE_TRADES, ACTIVE_POIS, LAST_SCAN_TIME
    with LOCK:
        if len(ACTIVE_TRADES) >= 2: return
        now = time.time()
        if now - LAST_SCAN_TIME < 60: return

        c15 = DATA_HUB["klines"].get("15m", [])
        c5 = DATA_HUB["klines"].get("5m", [])
        c1 = DATA_HUB["klines"].get("1m", [])
        p = DATA_HUB["price"]

        if len(c15) < 30 or len(c5) < 20 or len(c1) < 20: return

        a15 = calculate_atr(c15, 14)
        a1 = calculate_atr(c1, 14)

        # 1. 15M POI Scan
        fvg15 = detect_fvg(c15)
        sw15 = detect_sweep(c15, 8)
        ACTIVE_POIS = []
        if fvg15:
            ACTIVE_POIS.append({"type": "15M_FVG", "dir": fvg15["dir"], "low": fvg15["low"], "high": fvg15["high"]})

        # 2. Execution Setup (5M / 1M Confluence)
        direction = None
        setup_name = ""

        if sw15:
            direction = sw15["dir"]
            setup_name = "15M Liquidity Sweep + 1M MSS"
        elif fvg15:
            direction = fvg15["dir"]
            setup_name = "15M Institutional FVG Tap"

        if not direction: return

        # Micro Sweep on 1M/5M
        sw1 = detect_sweep(c1, 6)
        fvg1 = detect_fvg(c1)

        # Precise Entry & Tight SL
        entry = p
        if direction == "LONG":
            sl = (sw1["wick"] - 0.2 * a1) if sw1 else (entry - 1.2 * a15)
            risk = entry - sl
            if risk < 10.0 or risk > 250.0: return
            tp1 = entry + (2.0 * risk)
            tp2 = entry + (4.0 * risk)
            tp3 = entry + (6.0 * risk)
        else:
            sl = (sw1["wick"] + 0.2 * a1) if sw1 else (entry + 1.2 * a15)
            risk = sl - entry
            if risk < 10.0 or risk > 250.0: return
            tp1 = entry - (2.0 * risk)
            tp2 = entry - (4.0 * risk)
            tp3 = entry - (6.0 * risk)

        trade_type = "SNIPER" if sw1 else "NORMAL"
        t = {
            "id": f"TRD_{int(now*1000)}",
            "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "type": trade_type,
            "tf": "15M/1M",
            "dir": direction,
            "setup": setup_name,
            "score": 92 if sw1 else 80,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "tp3": round(tp3, 2),
            "realized_r": 0.0,
            "remaining_pct": 1.0,
            "stage": "OPEN"
        }

        ACTIVE_TRADES.append(t)
        LAST_SCAN_TIME = now

        send_telegram(
            f"⚡ <b>[{trade_type} ENTRY SIGNAL]</b>\n\n"
            f"<b>Direction:</b> {direction}\n"
            f"🔹 <b>Entry:</b> ${entry:,.2f}\n"
            f"🛑 <b>SL:</b> ${sl:,.2f} (Risk: ${risk:.1f})\n"
            f"🎯 <b>TP1 (2R):</b> ${tp1:,.2f}\n"
            f"🎯 <b>TP2 (4R):</b> ${tp2:,.2f}\n"
            f"🔥 <b>TP3 (6R):</b> ${tp3:,.2f}\n\n"
            f"💡 <b>Setup:</b> {setup_name}"
        )

# ---------------- 8. CONTINUOUS DAEMON LOOP ----------------
def engine_loop():
    global STARTED_FLAG
    time.sleep(3)
    if not STARTED_FLAG:
        send_telegram("🚀 <b>BTCUSDT SMC Engine Active 24/7</b>\nMonitoring Binance Futures live.")
        STARTED_FLAG = True

    while True:
        try:
            sync_data()
            p = DATA_HUB["price"]
            if p > 10000:
                manage_positions(p)
                scan_signals()
            time.sleep(3)
        except Exception as e:
            logging.error(f"Loop error: {e}")
            time.sleep(5)

threading.Thread(target=engine_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
