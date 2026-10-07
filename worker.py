import os
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [GAUTAM-JHA-2.0] %(message)s"
)

app = Flask(__name__)

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
BASE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

LOCK = threading.RLock()
STARTED_FLAG = False

DATA_HUB = {
    "price": 0.0,
    "pdh": 0.0,
    "pdl": 0.0,
    "session_tag": "ASIAN",
    "klines_15m": [],
    "last_sync": 0.0
}

ACTIVE_TRADES = []
ACTIVE_POIS = []

def db():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, created_at TEXT, signal_type TEXT, tf TEXT, direction TEXT,
        setup TEXT, score INTEGER, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        exit REAL, result TEXT, pnl_r REAL, status TEXT)""")
    c.commit()
    c.close()

init_db()

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: 
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        logging.error(f"Telegram alert error: {e}")

@app.route("/")
@app.route("/healthz")
def health():
    return "Gautam Jha 2.0 Engine Live", 200

@app.route("/vault-data")
def api_vault_data():
    try:
        c = db()
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        c.close()
        vault = [{
            "time": r["created_at"], "type": r["signal_type"], "tf": r["tf"],
            "dir": r["direction"], "setup": r["setup"], "score": r["score"],
            "entry": float(r["entry"] or 0), "exit": float(r["exit"] or 0),
            "res": r["result"], "pnl": float(r["pnl_r"] or 0), "status": r["status"]
        } for r in rows]
        with LOCK:
            return jsonify({
                "vault": vault, 
                "active": list(ACTIVE_TRADES), 
                "pois": list(ACTIVE_POIS),
                "price": DATA_HUB["price"],
                "session": DATA_HUB["session_tag"]
            }), 200
    except Exception:
        return jsonify({"vault": [], "active": [], "pois": []}), 500

def fetch_daily_pdh_pdl():
    try:
        r = requests.get(f"{BASE}/fapi/v1/klines", params={"symbol": SYMBOL, "interval": "1d", "limit": 3}, timeout=4).json()
        if isinstance(r, list) and len(r) >= 2:
            prev_day = r[-2] # Completed previous day candle
            return float(prev_day[2]), float(prev_day[3])
    except Exception:
        pass
    return 0.0, 0.0

def fetch_klines(interval="15m", limit=50):
    try:
        r = requests.get(f"{BASE}/fapi/v1/klines", params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=4).json()
        if isinstance(r, list) and len(r) > 10:
            parsed = []
            for b in r[:-1]: # Closed candles only
                parsed.append({
                    "time": int(b[0] // 1000), 
                    "open": float(b[1]), 
                    "high": float(b[2]), 
                    "low": float(b[3]), 
                    "close": float(b[4]), 
                    "vol": float(b[5])
                })
            return parsed
    except Exception:
        pass
    return []

def get_current_session():
    utc_hr = datetime.now(timezone.utc).hour
    if 0 <= utc_hr < 7: return "ASIAN (RANGE)"
    elif 7 <= utc_hr < 13: return "LONDON (TRAP / EXPANSION)"
    elif 13 <= utc_hr < 21: return "NEW YORK (TREND ENGINE)"
    return "SESSION CLOSE"

# ---------------- GAUTAM JHA 2.0 DECISION MATRIX ----------------
def scan_masterclass_2_setups(c15, pdh, pdl, p):
    if len(c15) < 15: 
        return None

    cur = c15[-1]
    prev = c15[-2]

    candle_range = max(cur["high"] - cur["low"], 1.0)
    body = abs(cur["close"] - cur["open"])
    lower_wick = min(cur["open"], cur["close"]) - cur["low"]
    upper_wick = cur["high"] - max(cur["open"], cur["close"])

    ref = c15[-14:-2]
    swing_high = max(x["high"] for x in ref)
    swing_low = min(x["low"] for x in ref)

    # 1. STRATEGY 2.0: PREVIOUS DAY LOW (PDL) REVERSAL TRAP
    if pdl > 0 and cur["low"] < pdl and cur["close"] > pdl:
        if cur["close"] > prev["high"] or (lower_wick / candle_range >= 0.35):
            sl = round(cur["low"] - 25.0, 2)
            risk = p - sl
            if 40.0 <= risk <= 650.0:
                return {
                    "dir": "LONG",
                    "category": "2.0_PDL_TRAP",
                    "setup": "Masterclass 2.0: PDL Swept + Green Confirmation (Retail Sellers Trapped)",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p + (2.0 * risk), 2),
                    "tp2": round(p + (3.5 * risk), 2),
                    "tp3": round(p + (5.0 * risk), 2)
                }

    # 2. STRATEGY 2.0: PREVIOUS DAY HIGH (PDH) REVERSAL TRAP
    if pdh > 0 and cur["high"] > pdh and cur["close"] < pdh:
        if cur["close"] < prev["low"] or (upper_wick / candle_range >= 0.35):
            sl = round(cur["high"] + 25.0, 2)
            risk = sl - p
            if 40.0 <= risk <= 650.0:
                return {
                    "dir": "SHORT",
                    "category": "2.0_PDH_TRAP",
                    "setup": "Masterclass 2.0: PDH Swept + Red Confirmation (Retail Buyers Trapped)",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p - (2.0 * risk), 2),
                    "tp2": round(p - (3.5 * risk), 2),
                    "tp3": round(p - (5.0 * risk), 2)
                }

    # 3. STRATEGY 1.0: SWING HIGH/LOW LIQUIDITY HUNT
    if cur["low"] < swing_low and cur["close"] > swing_low and cur["close"] > cur["open"]:
        sl = round(cur["low"] - 25.0, 2)
        risk = p - sl
        if 40.0 <= risk <= 650.0:
            return {
                "dir": "LONG",
                "category": "1.0_SWING_SWEEP",
                "setup": "Masterclass 1.0: Liquidity Hunt at Support Swing + Reversal Close",
                "entry": p,
                "sl": sl,
                "risk": risk,
                "tp1": round(p + (2.0 * risk), 2),
                "tp2": round(p + (3.5 * risk), 2),
                "tp3": round(p + (5.0 * risk), 2)
            }

    if cur["high"] > swing_high and cur["close"] < swing_high and cur["close"] < cur["open"]:
        sl = round(cur["high"] + 25.0, 2)
        risk = sl - p
        if 40.0 <= risk <= 650.0:
            return {
                "dir": "SHORT",
                "category": "1.0_SWING_SWEEP",
                "setup": "Masterclass 1.0: Liquidity Hunt at Resistance Swing + Rejection Close",
                "entry": p,
                "sl": sl,
                "risk": risk,
                "tp1": round(p - (2.0 * risk), 2),
                "tp2": round(p - (3.5 * risk), 2),
                "tp3": round(p - (5.0 * risk), 2)
            }

    # 4. TREND MOMENTUM BREAKOUT (Solid Body Expansion)
    if body / candle_range >= 0.65:
        if cur["close"] > swing_high and cur["close"] > cur["open"]:
            sl = round(cur["open"] - 25.0, 2)
            risk = p - sl
            if 40.0 <= risk <= 650.0:
                return {
                    "dir": "LONG",
                    "category": "2.0_MOMENTUM_RUN",
                    "setup": "Institutional Momentum Breakout (Closing above Resistance)",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p + (2.0 * risk), 2),
                    "tp2": round(p + (3.5 * risk), 2),
                    "tp3": round(p + (5.0 * risk), 2)
                }
        elif cur["close"] < swing_low and cur["close"] < cur["open"]:
            sl = round(cur["open"] + 25.0, 2)
            risk = sl - p
            if 40.0 <= risk <= 650.0:
                return {
                    "dir": "SHORT",
                    "category": "2.0_MOMENTUM_RUN",
                    "setup": "Institutional Waterfall Breakdown (Closing below Support)",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p - (2.0 * risk), 2),
                    "tp2": round(p - (3.5 * risk), 2),
                    "tp3": round(p - (5.0 * risk), 2)
                }

    return None

def sync_market_hub():
    global DATA_HUB, ACTIVE_POIS
    try:
        p_res = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["price"] = float(p_res.get("price", 0.0))

        # Fetch PDH & PDL
        pdh, pdl = fetch_daily_pdh_pdl()
        DATA_HUB["pdh"] = pdh
        DATA_HUB["pdl"] = pdl

        k15 = fetch_klines("15m", 45)
        if k15:
            DATA_HUB["klines_15m"] = k15
            with LOCK:
                ACTIVE_POIS = [
                    {"type": "PDH", "title": "2.0 PDH (Previous Day High)", "price": round(pdh, 2), "color": "#ff1744"},
                    {"type": "PDL", "title": "2.0 PDL (Previous Day Low)", "price": round(pdl, 2), "color": "#00e676"}
                ]

        DATA_HUB["session_tag"] = get_current_session()
        DATA_HUB["last_sync"] = time.time()
    except Exception as e:
        logging.warning(f"Sync issue: {e}")

def manage_positions(p):
    global ACTIVE_TRADES
    with LOCK:
        rem = []
        for t in ACTIVE_TRADES:
            entry = t["entry"]
            if t["dir"] == "LONG":
                if p >= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["sl"] = entry
                    send_telegram(f"🎯 <b>[{t['type']}] 1:2 TP1 HIT</b>\nStop Loss moved to <b>Breakeven (${entry:,.2f})</b>")
                elif p >= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] 1:3.5 TP2 HIT</b>\nStop Loss trailed to <b>TP1 (${t['tp1']:,.2f})</b>")
                if p <= t["sl"]:
                    pnl = -1.0 if t["stage"] == "OPEN" else 1.5
                    res = "SL HIT" if t["stage"] == "OPEN" else "PROFIT / BE CLOSE"
                    save_closed(t, p, res, pnl)
                    send_telegram(f"🏁 <b>[{t['type']} FINISHED] {res}</b> @ ${p:,.2f} | PnL: <b>{pnl:+.1f}R</b>")
                    continue
                elif p >= t["tp3"]:
                    save_closed(t, p, "RUNNER TARGET 1:5 🔥", 5.0)
                    send_telegram(f"🔥 <b>[{t['type']} 1:5 TARGET ACCOMPLISHED]</b> @ ${p:,.2f} | PnL: <b>+5.0R</b>")
                    continue

            elif t["dir"] == "SHORT":
                if p <= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["sl"] = entry
                    send_telegram(f"🎯 <b>[{t['type']}] 1:2 TP1 HIT</b>\nStop Loss moved to <b>Breakeven (${entry:,.2f})</b>")
                elif p <= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] 1:3.5 TP2 HIT</b>\nStop Loss trailed to <b>TP1 (${t['tp1']:,.2f})</b>")
                if p >= t["sl"]:
                    pnl = -1.0 if t["stage"] == "OPEN" else 1.5
                    res = "SL HIT" if t["stage"] == "OPEN" else "PROFIT / BE CLOSE"
                    save_closed(t, p, res, pnl)
                    send_telegram(f"🏁 <b>[{t['type']} FINISHED] {res}</b> @ ${p:,.2f} | PnL: <b>{pnl:+.1f}R</b>")
                    continue
                elif p <= t["tp3"]:
                    save_closed(t, p, "RUNNER TARGET 1:5 🔥", 5.0)
                    send_telegram(f"🔥 <b>[{t['type']} 1:5 TARGET ACCOMPLISHED]</b> @ ${p:,.2f} | PnL: <b>+5.0R</b>")
                    continue

            rem.append(t)
        ACTIVE_TRADES = rem

def save_closed(t, exit_p, res, pnl):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], 98,
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], exit_p, res, pnl, "CLOSED"))
        c.commit()
        c.close()
    except Exception as e:
        logging.error(f"DB save error: {e}")

LAST_SCAN_TIME = 0

def master_execution_scan():
    global ACTIVE_TRADES, LAST_SCAN_TIME
    with LOCK:
        if len(ACTIVE_TRADES) >= 1: 
            return
            
        now = time.time()
        if now - LAST_SCAN_TIME < 20: 
            return

        c15 = DATA_HUB["klines_15m"]
        p = DATA_HUB["price"]
        pdh = DATA_HUB["pdh"]
        pdl = DATA_HUB["pdl"]

        if len(c15) < 15 or p < 10000: 
            return

        signal = scan_masterclass_2_setups(c15, pdh, pdl, p)
        if not signal:
            return

        t = {
            "id": f"TRD_{int(now*1000)}",
            "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "type": signal["category"],
            "tf": "15M",
            "dir": signal["dir"],
            "setup": signal["setup"],
            "entry": round(signal["entry"], 2),
            "sl": round(signal["sl"], 2),
            "tp1": round(signal["tp1"], 2),
            "tp2": round(signal["tp2"], 2),
            "tp3": round(signal["tp3"], 2),
            "stage": "OPEN"
        }

        ACTIVE_TRADES.append(t)
        LAST_SCAN_TIME = now

        send_telegram(
            f"👑 <b>[GAUTAM JHA 2.0 STRATEGY ALERT]</b>\n\n"
            f"<b>Direction:</b> {signal['dir']} ({signal['category']})\n"
            f"🔹 <b>Entry:</b> ${signal['entry']:,.2f}\n"
            f"🛑 <b>Stop Loss:</b> ${signal['sl']:,.2f} (Risk: ${signal['risk']:.1f})\n"
            f"🎯 <b>Target 1 (1:2):</b> ${signal['tp1']:,.2f}\n"
            f"🎯 <b>Target 2 (1:3.5):</b> ${signal['tp2']:,.2f}\n"
            f"🔥 <b>Target 3 (1:5):</b> ${signal['tp3']:,.2f}\n\n"
            f"🧠 <b>Trading Logic:</b> {signal['setup']}\n"
            f"📍 <b>Key Daily Benchmark:</b> PDH: ${pdh:,.1f} | PDL: ${pdl:,.1f}\n"
            f"⏱ <b>Session:</b> {DATA_HUB['session_tag']}\n"
            f"🛡 <b>Auto Management:</b> Cost-to-cost Breakeven at TP1"
        )

def keep_alive_ping():
    time.sleep(60)
    while True:
        try:
            url = RENDER_EXTERNAL_URL or "http://127.0.0.1:10000/healthz"
            requests.get(url, timeout=5)
        except Exception:
            pass
        time.sleep(300)

def master_loop():
    global STARTED_FLAG
    time.sleep(3)
    if not STARTED_FLAG:
        send_telegram("👑 <b>GAUTAM JHA 2.0 ENGINE LIVE</b>\nPDH / PDL Sweeps & Confirmation Entries Active 24/7.")
        STARTED_FLAG = True

    while True:
        try:
            sync_market_hub()
            p = DATA_HUB["price"]
            if p > 10000:
                manage_positions(p)
                master_execution_scan()
            time.sleep(3)
        except Exception as e:
            logging.error(f"Loop issue: {e}")
            time.sleep(3)

threading.Thread(target=master_loop, daemon=True).start()
threading.Thread(target=keep_alive_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
