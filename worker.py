import os
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [MASTER-ENGINE] %(message)s")
app = Flask(__name__)

DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

LOCK = threading.RLock()
STARTED_FLAG = False
PROCESSED_TIMESTAMPS = set() # Hard dedup lock in memory

# Discipline Rules
MAX_DAILY_TRADES = 3
MAX_CONSECUTIVE_LOSSES = 2
DAILY_STATS = {"day": "", "trades_count": 0, "consecutive_losses": 0, "locked": False}

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DATA_HUB = {"price": 0.0, "pdh": 0.0, "pdl": 0.0, "session": "ASIAN", "klines": [], "last_sync": 0}
ACTIVE_TRADES = []
ACTIVE_POIS = []

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT UNIQUE, created_at TEXT, 
        closed_at TEXT, signal_type TEXT, tf TEXT, direction TEXT, setup TEXT, 
        entry REAL, sl REAL, tp1 REAL, tp2 REAL, exit REAL, result TEXT, 
        pnl_r REAL, pnl_percent REAL, status TEXT)""")
    conn.commit()
    conn.close()

init_db()

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        logging.error(f"TG Error: {e}")

@app.route("/")
@app.route("/healthz")
def health():
    return "Master Institutional Engine Live", 200

@app.route("/vault-data")
def api_vault():
    try:
        conn = db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        conn.close()
        vault = [dict(r) for r in rows]
        with LOCK:
            return jsonify({
                "vault": vault, "active": list(ACTIVE_TRADES), "pois": list(ACTIVE_POIS),
                "price": DATA_HUB["price"], "pdh": DATA_HUB["pdh"], "pdl": DATA_HUB["pdl"],
                "session": DATA_HUB["session"], "daily_stats": DAILY_STATS
            }), 200
    except Exception:
        return jsonify({"vault": [], "active": [], "pois": []}), 500

def fetch_data():
    global DATA_HUB, ACTIVE_POIS
    try:
        # Price
        rp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", headers=HEADERS, timeout=4).json()
        if "data" in rp: DATA_HUB["price"] = float(rp["data"]["amount"])

        # Daily (PDH/PDL)
        rd = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400", headers=HEADERS, timeout=4).json()
        if len(rd) >= 2: DATA_HUB["pdh"], DATA_HUB["pdl"] = float(rd[1][2]), float(rd[1][1])

        # 15M Klines (Liquidity & Volume)
        r15 = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900", headers=HEADERS, timeout=4).json()
        if len(r15) > 20:
            parsed = [{"time": int(b[0]), "low": float(b[1]), "high": float(b[2]), "open": float(b[3]), "close": float(b[4]), "vol": float(b[5])} for b in reversed(r15[1:50])]
            DATA_HUB["klines"] = parsed
            
            ref = parsed[-16:-2]
            with LOCK:
                ACTIVE_POIS = [
                    {"type": "PDH", "title": "Prev Day High (Liquidity)", "price": DATA_HUB["pdh"], "color": "#ff1744"},
                    {"type": "PDL", "title": "Prev Day Low (Liquidity)", "price": DATA_HUB["pdl"], "color": "#00e676"},
                    {"type": "BSL", "title": "Swing High (BSL Trap)", "price": max(x["high"] for x in ref), "color": "#ef5350"},
                    {"type": "SSL", "title": "Swing Low (SSL Trap)", "price": min(x["low"] for x in ref), "color": "#26a69a"}
                ]
    except:
        pass

def calculate_ema(closes, period=9):
    if len(closes) < period: return closes[-1] if closes else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]: ema = (p - ema) * multiplier + ema
    return ema

# --- CORE PSYCHOLOGY & LIQUIDITY LOGIC ---
def scan_institutional_trap(c15, pdh, pdl, p):
    if len(c15) < 20 or p < 10000: return None
    
    cur = c15[-1]  # Active/Confirmation candle
    prev = c15[-2] # Trap candle
    
    # 1. Math Filters
    ema9 = calculate_ema([x["close"] for x in c15], 9)
    avg_vol = sum(x["vol"] for x in c15[-12:-2]) / 10
    
    prev_range = max(prev["high"] - prev["low"], 1.0)
    cur_range = max(cur["high"] - cur["low"], 1.0)
    
    # Volatility Check (No extreme news spikes)
    if prev_range > 1200 or cur_range > 1200: return None
    
    upper_wick_pct = (prev["high"] - max(prev["open"], prev["close"])) / prev_range
    lower_wick_pct = (min(prev["open"], prev["close"]) - prev["low"]) / prev_range
    
    swing_high = max(x["high"] for x in c15[-16:-2])
    swing_low = min(x["low"] for x in c15[-16:-2])

    # SETUP 1: PDL / SSL LIQUIDITY HUNT (BUY SETUP)
    # Logic: Price sweeps below PDL/SSL, high volume absorption, closes back inside, strong wick (>=30%)
    if (pdl > 0 and prev["low"] < pdl and prev["close"] > pdl) or (prev["low"] < swing_low and prev["close"] > swing_low):
        if lower_wick_pct >= 0.30 and prev["vol"] > (avg_vol * 1.1) and cur["close"] > ema9:
            sl = round(prev["low"] - 15.0, 2)
            risk = round(p - sl, 2)
            if 30 <= risk <= 550:
                cat = "PDL_TRAP" if prev["low"] < pdl else "SSL_SWEEP"
                return {"dir": "LONG", "cat": cat, "setup": f"{cat}: Liquidity Hunt + 30% Wick Absorption + Volume Influx", "entry": p, "sl": sl, "risk": risk, "tp1": round(p + (2.0*risk), 2), "tp2": round(p + (3.5*risk), 2), "time": cur["time"]}

    # SETUP 2: PDH / BSL LIQUIDITY HUNT (SELL SETUP)
    # Logic: Price sweeps above PDH/BSL, high volume resistance, closes back inside, strong wick (>=30%)
    if (pdh > 0 and prev["high"] > pdh and prev["close"] < pdh) or (prev["high"] > swing_high and prev["close"] < swing_high):
        if upper_wick_pct >= 0.30 and prev["vol"] > (avg_vol * 1.1) and cur["close"] < ema9:
            sl = round(prev["high"] + 15.0, 2)
            risk = round(sl - p, 2)
            if 30 <= risk <= 550:
                cat = "PDH_TRAP" if prev["high"] > pdh else "BSL_SWEEP"
                return {"dir": "SHORT", "cat": cat, "setup": f"{cat}: Retail Breakout Trapped + 30% Wick Rejection", "entry": p, "sl": sl, "risk": risk, "tp1": round(p - (2.0*risk), 2), "tp2": round(p - (3.5*risk), 2), "time": cur["time"]}
    
    return None

def save_trade(t, status="OPEN", exit_p=0.0, res="RUNNING", pnl_r=0.0, pnl_pct=0.0):
    closed_at = datetime.now(timezone.utc).strftime("%H:%M:%S") if status == "CLOSED" else None
    conn = db()
    conn.execute("""INSERT OR REPLACE INTO trades 
        (trade_id, created_at, closed_at, signal_type, tf, direction, setup, entry, sl, tp1, tp2, exit, result, pnl_r, pnl_percent, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t["id"], t["created"], closed_at, t["cat"], "15M", t["dir"], t["setup"], t["entry"], t["sl"], t["tp1"], t["tp2"], exit_p, res, pnl_r, pnl_pct, status))
    conn.commit()
    conn.close()

def manage_positions(p):
    global ACTIVE_TRADES, DAILY_STATS
    with LOCK:
        rem = []
        for t in ACTIVE_TRADES:
            entry, sl, tp1, tp2, dr = t["entry"], t["sl"], t["tp1"], t["tp2"], t["dir"]
            
            # Risk-Free Auto Breakeven
            if (dr == "LONG" and p >= tp1) or (dr == "SHORT" and p <= tp1):
                if t.get("stage") != "TP1_DONE":
                    t["stage"] = "TP1_DONE"
                    t["sl"] = entry
                    save_trade(t, status="RISK_FREE")
                    send_telegram(f"🎯 <b>[TP1 HIT (+2R)]</b>\nStop Loss shifted to Entry (${entry:,.2f}). Trade is Risk-Free.")
            
            # SL or Breakeven Hit
            if (dr == "LONG" and p <= t["sl"]) or (dr == "SHORT" and p >= t["sl"]):
                full_loss = t.get("stage") != "TP1_DONE"
                pnl_r = -1.0 if full_loss else 0.0
                pnl_pct = round(((p - entry)/entry)*100, 2) if dr=="LONG" else round(((entry - p)/entry)*100, 2)
                res = "SL HIT (-1R)" if full_loss else "BREAKEVEN (0R)"
                
                DAILY_STATS["consecutive_losses"] = DAILY_STATS["consecutive_losses"] + 1 if full_loss else 0
                save_trade(t, status="CLOSED", exit_p=p, res=res, pnl_r=pnl_r, pnl_pct=pnl_pct)
                send_telegram(f"🏁 <b>[{t['cat']} CLOSED]</b> {res}\nExit: ${p:,.2f} | PnL: {pnl_pct:+.2f}%")
                continue
                
            # TP2 Runner Hit
            if (dr == "LONG" and p >= tp2) or (dr == "SHORT" and p <= tp2):
                DAILY_STATS["consecutive_losses"] = 0
                pnl_pct = round(((p - entry)/entry)*100, 2) if dr=="LONG" else round(((entry - p)/entry)*100, 2)
                save_trade(t, status="CLOSED", exit_p=p, res="TP2 HIT (+3.5R)", pnl_r=3.5, pnl_pct=pnl_pct)
                send_telegram(f"🔥 <b>[{t['cat']} TP2 ACHIEVED]</b>\nExit: ${p:,.2f} | PnL: {pnl_pct:+.2f}% (+3.5R)")
                continue

            rem.append(t)
        ACTIVE_TRADES = rem

def master_loop():
    global ACTIVE_TRADES, PROCESSED_TIMESTAMPS, DAILY_STATS, STARTED_FLAG
    time.sleep(2)
    if not STARTED_FLAG:
        send_telegram("👑 <b>MASTER ENGINE ONLINE</b>\nLiquidity Hunt + 30% Wick Absorption Rules Active. Zero-Duplicate DB Locked.")
        STARTED_FLAG = True

    while True:
        try:
            fetch_data()
            p = DATA_HUB["price"]
            if p < 10000: continue
            
            manage_positions(p)
            
            with LOCK:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if DAILY_STATS["day"] != today: DAILY_STATS = {"day": today, "trades_count": 0, "consecutive_losses": 0, "locked": False}
                
                if len(ACTIVE_TRADES) >= 1 or DAILY_STATS["trades_count"] >= MAX_DAILY_TRADES: 
                    time.sleep(3); continue
                
                if DAILY_STATS["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                    if not DAILY_STATS["locked"]:
                        DAILY_STATS["locked"] = True
                        send_telegram("🛑 <b>RISK LOCK:</b> 2 consecutive SL hits. Engine paused for the day.")
                    time.sleep(3); continue
                
                sig = scan_institutional_trap(DATA_HUB["klines"], DATA_HUB["pdh"], DATA_HUB["pdl"], p)
                if not sig: 
                    time.sleep(3); continue
                
                # --- ZERO DUPLICATE LOCK ---
                cts = sig["time"]
                tid = f"TRD_{cts}"
                if cts in PROCESSED_TIMESTAMPS: 
                    time.sleep(3); continue
                
                c = db()
                if c.execute("SELECT 1 FROM trades WHERE trade_id=?", (tid,)).fetchone():
                    PROCESSED_TIMESTAMPS.add(cts)
                    c.close(); time.sleep(3); continue
                c.close()
                
                # EXECUTE
                PROCESSED_TIMESTAMPS.add(cts)
                t = {"id": tid, "created": datetime.now(timezone.utc).strftime("%H:%M:%S"), "cat": sig["cat"], "dir": sig["dir"], "setup": sig["setup"], "entry": sig["entry"], "sl": sig["sl"], "tp1": sig["tp1"], "tp2": sig["tp2"], "stage": "OPEN"}
                ACTIVE_TRADES.append(t)
                DAILY_STATS["trades_count"] += 1
                
                save_trade(t, status="OPEN")
                send_telegram(f"👑 <b>[INSTITUTIONAL TRAP CONFIRMED]</b>\n\n<b>{sig['dir']}</b> ({sig['cat']})\n🔹 Entry: ${sig['entry']:,.2f}\n🛑 SL: ${sig['sl']:,.2f} (Risk: ${sig['risk']:.1f})\n🎯 TP1: ${sig['tp1']:,.2f}\n🔥 TP2: ${sig['tp2']:,.2f}\n\n🧠 Logic: {sig['setup']}\n🛡️ Rules: Strict 30% Rejection Wick + High Volume Influx Verified.")
            
            time.sleep(3)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(3)

def keep_alive():
    while True:
        try: requests.get("http://127.0.0.1:10000/healthz", timeout=5)
        except: pass
        time.sleep(300)

threading.Thread(target=master_loop, daemon=True).start()
threading.Thread(target=keep_alive, daemon=True).start()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
