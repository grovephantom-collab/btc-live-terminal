import os
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [MASTER-DB-ENGINE] %(message)s")
app = Flask(__name__)

DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DATA_HUB = {"price": 0.0, "pdh": 0.0, "pdl": 0.0, "session": "ASIAN", "klines": []}
ACTIVE_POIS = []

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT UNIQUE, created_date TEXT, created_at TEXT, 
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
    return "Master Institutional DB Engine Live", 200

@app.route("/vault-data")
def api_vault():
    try:
        conn = db()
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        active = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN', 'RISK_FREE')").fetchall()
        conn.close()
        
        return jsonify({
            "vault": [dict(r) for r in rows], 
            "active": [dict(r) for r in active], 
            "pois": ACTIVE_POIS,
            "price": DATA_HUB["price"], "pdh": DATA_HUB["pdh"], "pdl": DATA_HUB["pdl"],
            "session": DATA_HUB["session"]
        }), 200
    except Exception:
        return jsonify({"vault": [], "active": [], "pois": []}), 500

def fetch_data():
    global DATA_HUB, ACTIVE_POIS
    try:
        rp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", headers=HEADERS, timeout=4).json()
        if "data" in rp: DATA_HUB["price"] = float(rp["data"]["amount"])

        rd = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400", headers=HEADERS, timeout=4).json()
        if len(rd) >= 2: DATA_HUB["pdh"], DATA_HUB["pdl"] = float(rd[1][2]), float(rd[1][1])

        r15 = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900", headers=HEADERS, timeout=4).json()
        if len(r15) > 20:
            parsed = [{"time": int(b[0]), "low": float(b[1]), "high": float(b[2]), "open": float(b[3]), "close": float(b[4]), "vol": float(b[5])} for b in reversed(r15[1:50])]
            DATA_HUB["klines"] = parsed
            
            ref = parsed[-16:-2]
            ACTIVE_POIS = [
                {"type": "PDH", "title": "Prev Day High", "price": DATA_HUB["pdh"], "color": "#ff1744"},
                {"type": "PDL", "title": "Prev Day Low", "price": DATA_HUB["pdl"], "color": "#00e676"},
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

def scan_institutional_trap(c15, pdh, pdl, p):
    if len(c15) < 20 or p < 10000: return None
    
    cur, prev = c15[-1], c15[-2]
    ema9 = calculate_ema([x["close"] for x in c15], 9)
    avg_vol = sum(x["vol"] for x in c15[-12:-2]) / 10
    
    prev_range = max(prev["high"] - prev["low"], 1.0)
    if prev_range > 1200 or max(cur["high"] - cur["low"], 1.0) > 1200: return None
    
    upper_wick = (prev["high"] - max(prev["open"], prev["close"])) / prev_range
    lower_wick = (min(prev["open"], prev["close"]) - prev["low"]) / prev_range
    
    swing_high = max(x["high"] for x in c15[-16:-2])
    swing_low = min(x["low"] for x in c15[-16:-2])

    if (pdl > 0 and prev["low"] < pdl and prev["close"] > pdl) or (prev["low"] < swing_low and prev["close"] > swing_low):
        if lower_wick >= 0.28 and prev["vol"] > (avg_vol * 1.1) and cur["close"] > ema9:
            sl, risk = round(prev["low"] - 15.0, 2), round(p - (prev["low"] - 15.0), 2)
            if 30 <= risk <= 550:
                cat = "PDL_TRAP" if prev["low"] < pdl else "SSL_SWEEP"
                return {"dir": "LONG", "cat": cat, "setup": f"{cat}: Liquidity Hunt + Wick Rejection", "entry": p, "sl": sl, "risk": risk, "tp1": round(p + (2.0*risk), 2), "tp2": round(p + (3.5*risk), 2), "time": cur["time"]}

    if (pdh > 0 and prev["high"] > pdh and prev["close"] < pdh) or (prev["high"] > swing_high and prev["close"] < swing_high):
        if upper_wick >= 0.28 and prev["vol"] > (avg_vol * 1.1) and cur["close"] < ema9:
            sl, risk = round(prev["high"] + 15.0, 2), round((prev["high"] + 15.0) - p, 2)
            if 30 <= risk <= 550:
                cat = "PDH_TRAP" if prev["high"] > pdh else "BSL_SWEEP"
                return {"dir": "SHORT", "cat": cat, "setup": f"{cat}: Breakout Trapped + Wick Rejection", "entry": p, "sl": sl, "risk": risk, "tp1": round(p - (2.0*risk), 2), "tp2": round(p - (3.5*risk), 2), "time": cur["time"]}
    
    return None

def manage_positions(p):
    conn = db()
    open_trades = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN', 'RISK_FREE')").fetchall()
    
    for t in open_trades:
        tid, dr, entry, sl, tp1, tp2, status = t["trade_id"], t["direction"], t["entry"], t["sl"], t["tp1"], t["tp2"], t["status"]
        
        # Risk-Free Shift (TP1 Hit)
        if status == "OPEN":
            if (dr == "LONG" and p >= tp1) or (dr == "SHORT" and p <= tp1):
                # Atomic Update: Only 1 worker can successfully update this
                res = conn.execute("UPDATE trades SET status='RISK_FREE', sl=? WHERE trade_id=? AND status='OPEN'", (entry, tid))
                conn.commit()
                if res.rowcount > 0:
                    send_telegram(f"🎯 <b>[TP1 HIT (+2R)]</b>\nStop Loss auto-shifted to Entry (${entry:,.2f}). Trade is now completely Risk-Free.")
        
        # SL Hit / Breakeven Hit
        if (dr == "LONG" and p <= sl) or (dr == "SHORT" and p >= sl):
            full_loss = (status == "OPEN")
            pnl_r = -1.0 if full_loss else 0.0
            pnl_pct = round(((p - entry)/entry)*100, 2) if dr=="LONG" else round(((entry - p)/entry)*100, 2)
            res_text = "SL HIT (-1R)" if full_loss else "BREAKEVEN EXIT (0R)"
            
            res = conn.execute("UPDATE trades SET status='CLOSED', exit=?, result=?, pnl_r=?, pnl_percent=? WHERE trade_id=? AND status!='CLOSED'", (p, res_text, pnl_r, pnl_pct, tid))
            conn.commit()
            if res.rowcount > 0:
                send_telegram(f"🏁 <b>[{t['signal_type']} CLOSED]</b> {res_text}\nExit: ${p:,.2f} | PnL: <b>{pnl_pct:+.2f}%</b>")
                
        # TP2 Runner Hit
        elif (dr == "LONG" and p >= tp2) or (dr == "SHORT" and p <= tp2):
            pnl_pct = round(((p - entry)/entry)*100, 2) if dr=="LONG" else round(((entry - p)/entry)*100, 2)
            res = conn.execute("UPDATE trades SET status='CLOSED', exit=?, result=?, pnl_r=?, pnl_percent=? WHERE trade_id=? AND status!='CLOSED'", (p, "TP2 HIT (+3.5R)", 3.5, pnl_pct, tid))
            conn.commit()
            if res.rowcount > 0:
                send_telegram(f"🔥 <b>[{t['signal_type']} TP2 ACHIEVED]</b>\nExit: ${p:,.2f} | PnL: <b>{pnl_pct:+.2f}%</b> (+3.5R)")
    conn.close()

def master_loop():
    while True:
        try:
            fetch_data()
            p = DATA_HUB["price"]
            if p < 10000: 
                time.sleep(3); continue
            
            manage_positions(p)
            
            # --- Daily Discipline Limits Check (Direct from DB) ---
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            conn = db()
            day_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE created_date=?", (today,)).fetchone()[0]
            if day_trades >= 3:
                conn.close(); time.sleep(4); continue
                
            last_two = conn.execute("SELECT result FROM trades WHERE created_date=? AND status='CLOSED' ORDER BY id DESC LIMIT 2", (today,)).fetchall()
            if len(last_two) == 2 and "SL HIT" in last_two[0]["result"] and "SL HIT" in last_two[1]["result"]:
                conn.close(); time.sleep(4); continue
            conn.close()
            
            sig = scan_institutional_trap(DATA_HUB["klines"], DATA_HUB["pdh"], DATA_HUB["pdl"], p)
            if not sig: 
                time.sleep(4); continue
            
            tid = f"TRD_{sig['time']}"
            t_time = datetime.now(timezone.utc).strftime("%H:%M:%S")
            
            # --- ZERO DUPLICATE ATOMIC INSERT ---
            try:
                conn = db()
                conn.execute("""INSERT INTO trades (trade_id, created_date, created_at, signal_type, tf, direction, setup, entry, sl, tp1, tp2, status) 
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", 
                             (tid, today, t_time, sig["cat"], "15M", sig["dir"], sig["setup"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], "OPEN"))
                conn.commit()
                # Ye message sirf usi ek worker se jayega jisne database me first entry ki hogi
                send_telegram(f"👑 <b>[INSTITUTIONAL TRAP CONFIRMED]</b>\n\n<b>Direction: {sig['dir']}</b> ({sig['cat']})\n🔹 Entry: ${sig['entry']:,.2f}\n🛑 SL: ${sig['sl']:,.2f} (Risk: ${sig['risk']:.1f})\n🎯 TP1 (1:2): ${sig['tp1']:,.2f}\n🔥 TP2 (1:3.5): ${sig['tp2']:,.2f}\n\n🧠 Logic: {sig['setup']}\n🛡️ Auto-Mgmt: Cost-to-cost SL at TP1")
            except sqlite3.IntegrityError:
                # Agar doosra worker same millisecond pe aayega, toh DB usko error dekar reject kar dega (No duplicate telegram message!)
                pass
            finally:
                conn.close()
            
            time.sleep(3)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(3)

threading.Thread(target=master_loop, daemon=True).start()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
