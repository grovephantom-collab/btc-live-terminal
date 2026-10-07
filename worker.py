import os
import time
import math
import sqlite3
import logging
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [PRO-TRADER-AI-ENGINE] %(message)s"
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
    "funding_rate": 0.0,
    "book_imbalance": 0.0,
    "session_tag": "ASIAN",
    "klines": {"15m": []},
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
    return "BTC Institutional AI Engine Active", 200

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

def fetch_klines(interval="15m", limit=60):
    try:
        r = requests.get(f"{BASE}/fapi/v1/klines", params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=4).json()
        if isinstance(r, list) and len(r) > 10:
            parsed = []
            for b in r[:-1]:
                parsed.append({
                    "time": int(b[0] // 1000), 
                    "open": float(b[1]), 
                    "high": float(b[2]),
                    "low": float(b[3]), 
                    "close": float(b[4]), 
                    "vol": float(b[5]),
                    "taker_buy": float(b[9]) if len(b) > 9 else float(b[5]) * 0.5
                })
            return parsed
    except Exception:
        pass
    return []

def get_current_session():
    utc_hr = datetime.now(timezone.utc).hour
    if 0 <= utc_hr < 7: return "ASIAN (RANGE ACCUMULATION)"
    elif 7 <= utc_hr < 13: return "LONDON (MANIPULATION / EXPANSION)"
    elif 13 <= utc_hr < 21: return "NEW YORK (TREND EXECUTION)"
    return "US CLOSE"

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 60.0
    trs = [max(c["high"] - c["low"], abs(c["high"] - candles[i-1]["close"]), abs(c["low"] - candles[i-1]["close"])) 
           for i, c in enumerate(candles) if i > 0]
    return max(sum(trs[-period:]) / period, 15.0)

# ================= PRO TRADER REASONING MATRIX =================
def evaluate_pro_trader_narrative(candles, current_price):
    if len(candles) < 20: 
        return None

    cur = candles[-1]
    prev = candles[-2]
    atr = calculate_atr(candles, 14)

    # 1. Structural Benchmarks
    swing_pool = candles[-15:-1]
    swing_high = max(x["high"] for x in swing_pool)
    swing_low = min(x["low"] for x in swing_pool)
    avg_vol = sum(x["vol"] for x in swing_pool) / len(swing_pool)

    # Candle Anatomy
    total_range = max(cur["high"] - cur["low"], 1.0)
    body = abs(cur["close"] - cur["open"])
    upper_wick = cur["high"] - max(cur["open"], cur["close"])
    lower_wick = min(cur["open"], cur["close"]) - cur["low"]
    
    vol_surge = cur["vol"] > (1.2 * avg_vol)
    bullish_candle = cur["close"] > cur["open"]
    bearish_candle = cur["close"] < cur["open"]

    long_score = 0
    short_score = 0
    signals_notes = []

    # CONTEXT 1: LIQUIDITY SWEEP & SPRING TRAP (Institutional Reversal)
    if cur["low"] < swing_low and cur["close"] > swing_low:
        long_score += 45
        signals_notes.append("SSL Swept (Retail Stop Runs Absorbed)")
        if lower_wick / total_range >= 0.35:
            long_score += 25
            signals_notes.append("Rejection Wick Confirmed")
    
    if cur["high"] > swing_high and cur["close"] < swing_high:
        short_score += 45
        signals_notes.append("BSL Swept (Retail Breakout Buyers Trapped)")
        if upper_wick / total_range >= 0.35:
            short_score += 25
            signals_notes.append("Bearish Exhaustion Wick Confirmed")

    # CONTEXT 2: WATERFALL MOMENTUM & DISPLACEMENT EXPANSION (Trend Run)
    if bearish_candle and body / total_range >= 0.60 and cur["close"] < swing_low:
        short_score += 55
        signals_notes.append("Institutional Waterfall Displacement below Range")
        if vol_surge:
            short_score += 20
            signals_notes.append("High Volume Panic Dump")

    if bullish_candle and body / total_range >= 0.60 and cur["close"] > swing_high:
        long_score += 55
        signals_notes.append("Institutional Breakout Expansion above Range")
        if vol_surge:
            long_score += 20
            signals_notes.append("High Volume Aggressive Bid Expansion")

    # CONTEXT 3: FVG IMBALANCE TAPS (SMC Continuation)
    c1, c3 = candles[-3], cur
    if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) > 10.0:
        if cur["close"] > c3["open"]:
            long_score += 30
            signals_notes.append("Bullish FVG Continuation")
    elif c1["low"] > c3["high"] and (c1["low"] - c3["high"]) > 10.0:
        if cur["close"] < c3["open"]:
            short_score += 30
            signals_notes.append("Bearish FVG Continuation")

    # Decision Threshold
    if long_score >= 65 and long_score > short_score:
        risk_dist = max(min(1.2 * atr, 650.0), 40.0)
        return {
            "dir": "LONG",
            "score": min(long_score, 99),
            "category": "PRO_SMC_LONG",
            "setup": " + ".join(signals_notes),
            "entry": current_price,
            "sl": round(current_price - risk_dist, 2),
            "risk": round(risk_dist, 2),
            "tp1": round(current_price + (2.0 * risk_dist), 2),
            "tp2": round(current_price + (3.5 * risk_dist), 2),
            "tp3": round(current_price + (5.0 * risk_dist), 2)
        }
    elif short_score >= 65 and short_score > long_score:
        risk_dist = max(min(1.2 * atr, 650.0), 40.0)
        return {
            "dir": "SHORT",
            "score": min(short_score, 99),
            "category": "PRO_SMC_SHORT",
            "setup": " + ".join(signals_notes),
            "entry": current_price,
            "sl": round(current_price + risk_dist, 2),
            "risk": round(risk_dist, 2),
            "tp1": round(current_price - (2.0 * risk_dist), 2),
            "tp2": round(current_price - (3.5 * risk_dist), 2),
            "tp3": round(current_price - (5.0 * risk_dist), 2)
        }
    return None

def sync_market_hub():
    global DATA_HUB, ACTIVE_POIS
    try:
        p_res = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["price"] = float(p_res.get("price", 0.0))

        k15 = fetch_klines("15m", 50)
        if k15:
            DATA_HUB["klines"]["15m"] = k15
            ref = k15[-16:]
            with LOCK:
                ACTIVE_POIS = [
                    {"type": "BSL", "title": "BSL (Liquidity High)", "price": round(max(x["high"] for x in ref), 2), "color": "#ef5350"},
                    {"type": "SSL", "title": "SSL (Liquidity Low)", "price": round(min(x["low"] for x in ref), 2), "color": "#26a69a"}
                ]

        fr_res = requests.get(f"{BASE}/fapi/v1/premiumIndex", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["funding_rate"] = float(fr_res.get("lastFundingRate", 0.0))
        DATA_HUB["session_tag"] = get_current_session()
        DATA_HUB["last_sync"] = time.time()
    except Exception as e:
        logging.warning(f"Sync warning: {e}")

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
                    send_telegram(f"🎯 <b>[{t['type']}] TP1 HIT (+2.0R)</b>\nStop Loss moved to Breakeven (${entry:,.2f})")
                elif p >= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] TP2 HIT (+3.5R)</b>\nStop Loss trailed to TP1 (${t['tp1']:,.2f})")
                if p <= t["sl"]:
                    pnl = -1.0 if t["stage"] == "OPEN" else 1.5
                    res = "SL HIT" if t["stage"] == "OPEN" else "BE/PROFIT CLOSE"
                    save_closed(t, p, res, pnl)
                    send_telegram(f"🏁 <b>[{t['type']} CLOSED] {res}</b> @ ${p:,.2f} | Net: {pnl:+.1f}R")
                    continue
                elif p >= t["tp3"]:
                    save_closed(t, p, "TP3 COMPLETED 🔥", 5.0)
                    send_telegram(f"🔥 <b>[{t['type']} RUNNER TARGET HIT]</b> @ ${p:,.2f} | Net: +5.0R")
                    continue

            elif t["dir"] == "SHORT":
                if p <= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["sl"] = entry
                    send_telegram(f"🎯 <b>[{t['type']}] TP1 HIT (+2.0R)</b>\nStop Loss moved to Breakeven (${entry:,.2f})")
                elif p <= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] TP2 HIT (+3.5R)</b>\nStop Loss trailed to TP1 (${t['tp1']:,.2f})")
                if p >= t["sl"]:
                    pnl = -1.0 if t["stage"] == "OPEN" else 1.5
                    res = "SL HIT" if t["stage"] == "OPEN" else "BE/PROFIT CLOSE"
                    save_closed(t, p, res, pnl)
                    send_telegram(f"🏁 <b>[{t['type']} CLOSED] {res}</b> @ ${p:,.2f} | Net: {pnl:+.1f}R")
                    continue
                elif p <= t["tp3"]:
                    save_closed(t, p, "TP3 COMPLETED 🔥", 5.0)
                    send_telegram(f"🔥 <b>[{t['type']} RUNNER TARGET HIT]</b> @ ${p:,.2f} | Net: +5.0R")
                    continue

            rem.append(t)
        ACTIVE_TRADES = rem

def save_closed(t, exit_p, res, pnl):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], t["score"],
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

        c15 = DATA_HUB["klines"].get("15m", [])
        p = DATA_HUB["price"]

        if len(c15) < 15 or p < 10000: 
            return

        signal = evaluate_pro_trader_narrative(c15, p)
        if not signal:
            return

        t = {
            "id": f"TRD_{int(now*1000)}",
            "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "type": signal["category"],
            "tf": "15M",
            "dir": signal["dir"],
            "setup": signal["setup"],
            "score": signal["score"],
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
            f"🧠 <b>[PRO TRADER NARRATIVE SIGNAL]</b>\n\n"
            f"<b>Direction:</b> {signal['dir']} (Confluence Score: {signal['score']}/100)\n"
            f"🔹 <b>Entry:</b> ${signal['entry']:,.2f}\n"
            f"🛑 <b>SL:</b> ${signal['sl']:,.2f} (Risk: ${signal['risk']:.1f})\n"
            f"🎯 <b>TP1 (2.0R):</b> ${signal['tp1']:,.2f}\n"
            f"🎯 <b>TP2 (3.5R):</b> ${signal['tp2']:,.2f}\n"
            f"🔥 <b>TP3 (5.0R):</b> ${signal['tp3']:,.2f}\n\n"
            f"💡 <b>Market Narrative:</b> {signal['setup']}\n"
            f"⏱ <b>Session:</b> {DATA_HUB['session_tag']}\n"
            f"🛡 <b>Trailing Protection:</b> Auto-Breakeven at TP1"
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
        send_telegram("🧠 <b>PRO TRADER REASONING ENGINE ONLINE</b>\nMulti-Factor SMC, Liquidity Traps & Candle Psychology Active.")
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
