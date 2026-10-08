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
    format="%(asctime)s [%(levelname)s] [JHA-MASTER-PRO] %(message)s"
)

app = Flask(__name__)

DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

LOCK = threading.RLock()
STARTED_FLAG = False

# Hard Dedup Memory
PROCESSED_CANDLES = set()

MAX_DAILY_TRADES = 3
MAX_CONSECUTIVE_LOSSES = 2
DAILY_STATS = {
    "day": "",
    "trades_count": 0,
    "consecutive_losses": 0,
    "locked": False
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

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
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, 
        created_at TEXT, 
        closed_at TEXT,
        signal_type TEXT, 
        tf TEXT, 
        direction TEXT,
        setup TEXT, 
        entry REAL, 
        sl REAL, 
        tp1 REAL, 
        tp2 REAL, 
        exit REAL, 
        result TEXT, 
        pnl_r REAL, 
        pnl_percent REAL,
        status TEXT)""")
    conn.commit()
    conn.close()

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
    return "Institutional Discipline Engine Live", 200

@app.route("/vault-data")
def api_vault_data():
    try:
        conn = db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        conn.close()
        vault = [{
            "trade_id": r["trade_id"],
            "created_at": r["created_at"],
            "closed_at": r["closed_at"] or "--",
            "type": r["signal_type"],
            "dir": r["direction"],
            "setup": r["setup"],
            "entry": float(r["entry"] or 0),
            "sl": float(r["sl"] or 0),
            "tp1": float(r["tp1"] or 0),
            "tp2": float(r["tp2"] or 0),
            "exit": float(r["exit"] or 0),
            "res": r["result"],
            "pnl_r": float(r["pnl_r"] or 0),
            "pnl_percent": float(r["pnl_percent"] or 0),
            "status": r["status"]
        } for r in rows]
        with LOCK:
            return jsonify({
                "vault": vault, 
                "active": list(ACTIVE_TRADES), 
                "pois": list(ACTIVE_POIS),
                "price": DATA_HUB["price"],
                "pdh": DATA_HUB["pdh"],
                "pdl": DATA_HUB["pdl"],
                "session": DATA_HUB["session_tag"],
                "daily_stats": DAILY_STATS
            }), 200
    except Exception as e:
        logging.error(f"API vault error: {e}")
        return jsonify({"vault": [], "active": [], "pois": []}), 500

def fetch_coinbase_price():
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        r = requests.get(url, headers=HEADERS, timeout=4).json()
        if "data" in r and "amount" in r["data"]:
            return float(r["data"]["amount"])
    except Exception:
        pass
    return 0.0

def fetch_coinbase_15m():
    try:
        url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900"
        r = requests.get(url, headers=HEADERS, timeout=4).json()
        if isinstance(r, list) and len(r) > 15:
            parsed = []
            for b in reversed(r[1:50]):
                parsed.append({
                    "time": int(b[0]),
                    "low": float(b[1]),
                    "high": float(b[2]),
                    "open": float(b[3]),
                    "close": float(b[4]),
                    "vol": float(b[5])
                })
            return parsed
    except Exception:
        pass
    return []

def fetch_coinbase_daily():
    try:
        url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"
        r = requests.get(url, headers=HEADERS, timeout=4).json()
        if isinstance(r, list) and len(r) >= 2:
            prev_d = r[1]
            return float(prev_d[2]), float(prev_d[1])
    except Exception:
        pass
    return 0.0, 0.0

def calculate_ema(closes, period=9):
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def get_current_session():
    utc_hr = datetime.now(timezone.utc).hour
    if 0 <= utc_hr < 7: return "ASIAN RANGE"
    elif 7 <= utc_hr < 13: return "LONDON TRAP/EXPANSION"
    elif 13 <= utc_hr < 21: return "NEW YORK TREND"
    return "SESSION CLOSE"

def reset_daily_stats_if_needed():
    global DAILY_STATS
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if DAILY_STATS["day"] != today:
        DAILY_STATS["day"] = today
        DAILY_STATS["trades_count"] = 0
        DAILY_STATS["consecutive_losses"] = 0
        DAILY_STATS["locked"] = False

def scan_jha_masterclass_setups(c15, pdh, pdl, p):
    if len(c15) < 18 or p <= 1000.0: 
        return None

    cur = c15[-1]
    prev = c15[-2]

    closes = [x["close"] for x in c15]
    ema9 = calculate_ema(closes, period=9)

    cur_range = max(cur["high"] - cur["low"], 1.0)
    prev_range = max(prev["high"] - prev["low"], 1.0)

    if cur_range > 1200.0 or prev_range > 1200.0:
        return None

    prev_lower_wick = min(prev["open"], prev["close"]) - prev["low"]
    prev_upper_wick = prev["high"] - max(prev["open"], prev["close"])

    ref = c15[-16:-2]
    swing_high = max(x["high"] for x in ref)
    swing_low = min(x["low"] for x in ref)

    # 1. 2.0 PDL LIQUIDITY TRAP
    if pdl > 0 and prev["low"] < pdl and prev["close"] > pdl:
        if (prev_lower_wick / prev_range >= 0.28) and (cur["close"] > prev["high"]) and (cur["close"] > ema9):
            sl = round(prev["low"] - 25.0, 2)
            risk = round(p - sl, 2)
            if 30.0 <= risk <= 650.0:
                return {
                    "dir": "LONG",
                    "category": "2.0_PDL_TRAP",
                    "setup": "PDL Sweep + Absorption Wick + 9 EMA Bullish Confirmation",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p + (2.0 * risk), 2),
                    "tp2": round(p + (3.5 * risk), 2),
                    "candle_time": cur["time"]
                }

    # 2. 2.0 PDH LIQUIDITY TRAP
    if pdh > 0 and prev["high"] > pdh and prev["close"] < pdh:
        if (prev_upper_wick / prev_range >= 0.28) and (cur["close"] < prev["low"]) and (cur["close"] < ema9):
            sl = round(prev["high"] + 25.0, 2)
            risk = round(sl - p, 2)
            if 30.0 <= risk <= 650.0:
                return {
                    "dir": "SHORT",
                    "category": "2.0_PDH_TRAP",
                    "setup": "PDH Sweep + Rejection Wick + 9 EMA Bearish Confirmation",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p - (2.0 * risk), 2),
                    "tp2": round(p - (3.5 * risk), 2),
                    "candle_time": cur["time"]
                }

    # 3. 1.0 SWING SUPPORT TRAP
    if prev["low"] < swing_low and prev["close"] > swing_low:
        if (cur["close"] > prev["high"]) and (cur["close"] > cur["open"]) and (cur["close"] > ema9):
            sl = round(prev["low"] - 25.0, 2)
            risk = round(p - sl, 2)
            if 30.0 <= risk <= 650.0:
                return {
                    "dir": "LONG",
                    "category": "1.0_SWING_SWEEP",
                    "setup": "Support Swing Trap + Breakout Confirmation above 9 EMA",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p + (2.0 * risk), 2),
                    "tp2": round(p + (3.5 * risk), 2),
                    "candle_time": cur["time"]
                }

    # 4. 1.0 SWING RESISTANCE TRAP
    if prev["high"] > swing_high and prev["close"] < swing_high:
        if (cur["close"] < prev["low"]) and (cur["close"] < cur["open"]) and (cur["close"] < ema9):
            sl = round(prev["high"] + 25.0, 2)
            risk = round(sl - p, 2)
            if 30.0 <= risk <= 650.0:
                return {
                    "dir": "SHORT",
                    "category": "1.0_SWING_SWEEP",
                    "setup": "Resistance Swing Trap + Breakdown Confirmation below 9 EMA",
                    "entry": p,
                    "sl": sl,
                    "risk": risk,
                    "tp1": round(p - (2.0 * risk), 2),
                    "tp2": round(p - (3.5 * risk), 2),
                    "candle_time": cur["time"]
                }

    return None

def sync_market_hub():
    global DATA_HUB, ACTIVE_POIS
    try:
        p = fetch_coinbase_price()
        if p > 0.0:
            DATA_HUB["price"] = p

        pdh, pdl = fetch_coinbase_daily()
        if pdh > 0.0 and pdl > 0.0:
            DATA_HUB["pdh"] = pdh
            DATA_HUB["pdl"] = pdl

        k15 = fetch_coinbase_15m()
        if k15:
            DATA_HUB["klines_15m"] = k15
            ref = k15[-14:]
            with LOCK:
                ACTIVE_POIS = [
                    {"type": "PDH", "title": "PDH (High)", "price": round(pdh, 2), "color": "#ff1744"},
                    {"type": "PDL", "title": "PDL (Low)", "price": round(pdl, 2), "color": "#00e676"},
                    {"type": "BSL", "title": "Resistance Swing", "price": round(max(x["high"] for x in ref), 2), "color": "#ef5350"},
                    {"type": "SSL", "title": "Support Swing", "price": round(min(x["low"] for x in ref), 2), "color": "#26a69a"}
                ]

        DATA_HUB["session_tag"] = get_current_session()
        DATA_HUB["last_sync"] = time.time()
    except Exception as e:
        logging.warning(f"Sync error: {e}")

def save_or_update_trade(t, status="OPEN", exit_p=0.0, res="RUNNING", pnl_r=0.0, pnl_pct=0.0):
    try:
        closed_at = datetime.now(timezone.utc).strftime("%H:%M:%S") if status == "CLOSED" else None
        conn = db()
        conn.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, closed_at, signal_type, tf, direction, setup, entry, sl, tp1, tp2, exit, result, pnl_r, pnl_percent, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], closed_at, t["type"], t["tf"], t["dir"], t["setup"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], exit_p, res, pnl_r, pnl_pct, status))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"DB error: {e}")

def manage_positions(p):
    global ACTIVE_TRADES, DAILY_STATS
    with LOCK:
        rem = []
        for t in ACTIVE_TRADES:
            entry = t["entry"]
            direction = t["dir"]

            if direction == "LONG":
                if p >= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["sl"] = entry
                    save_or_update_trade(t, status="RUNNING_RISK_FREE")
                    send_telegram(
                        f"🎯 <b>[{t['type']}] 1:2 TARGET HIT</b>\n"
                        f"🛡 <b>Auto-Breakeven:</b> Stop Loss shifted to Entry (${entry:,.2f}). Trade is Risk-Free!"
                    )

                if p <= t["sl"]:
                    is_full_loss = (t["stage"] == "OPEN")
                    pnl_r = -1.0 if is_full_loss else 0.0
                    pnl_pct = round(((p - entry) / entry) * 100, 2)
                    res = "SL HIT (-1R)" if is_full_loss else "BREAKEVEN EXIT (0R)"
                    
                    if is_full_loss:
                        DAILY_STATS["consecutive_losses"] += 1
                    else:
                        DAILY_STATS["consecutive_losses"] = 0

                    save_or_update_trade(t, status="CLOSED", exit_p=p, res=res, pnl_r=pnl_r, pnl_pct=pnl_pct)
                    send_telegram(f"🏁 <b>[{t['type']} FINISHED] {res}</b> @ ${p:,.2f} | PnL: <b>{pnl_pct:+.2f}%</b> ({pnl_r:+.1f}R)")
                    continue

                elif p >= t["tp2"]:
                    DAILY_STATS["consecutive_losses"] = 0
                    pnl_pct = round(((p - entry) / entry) * 100, 2)
                    save_or_update_trade(t, status="CLOSED", exit_p=p, res="TP2 RUNNER HIT 🔥", pnl_r=3.5, pnl_pct=pnl_pct)
                    send_telegram(f"🔥 <b>[{t['type']} RUNNER TARGET HIT]</b> @ ${p:,.2f} | PnL: <b>{pnl_pct:+.2f}%</b> (+3.5R)")
                    continue

            elif direction == "SHORT":
                if p <= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["sl"] = entry
                    save_or_update_trade(t, status="RUNNING_RISK_FREE")
                    send_telegram(
                        f"🎯 <b>[{t['type']}] 1:2 TARGET HIT</b>\n"
                        f"🛡 <b>Auto-Breakeven:</b> Stop Loss shifted to Entry (${entry:,.2f}). Trade is Risk-Free!"
                    )

                if p <= t["sl"]:
                    is_full_loss = (t["stage"] == "OPEN")
                    pnl_r = -1.0 if is_full_loss else 0.0
                    pnl_pct = round(((entry - p) / entry) * 100, 2)
                    res = "SL HIT (-1R)" if is_full_loss else "BREAKEVEN EXIT (0R)"
                    
                    if is_full_loss:
                        DAILY_STATS["consecutive_losses"] += 1
                    else:
                        DAILY_STATS["consecutive_losses"] = 0

                    save_or_update_trade(t, status="CLOSED", exit_p=p, res=res, pnl_r=pnl_r, pnl_pct=pnl_pct)
                    send_telegram(f"🏁 <b>[{t['type']} FINISHED] {res}</b> @ ${p:,.2f} | PnL: <b>{pnl_pct:+.2f}%</b> ({pnl_r:+.1f}R)")
                    continue

                elif p <= t["tp2"]:
                    DAILY_STATS["consecutive_losses"] = 0
                    pnl_pct = round(((entry - p) / entry) * 100, 2)
                    save_or_update_trade(t, status="CLOSED", exit_p=p, res="TP2 RUNNER HIT 🔥", pnl_r=3.5, pnl_pct=pnl_pct)
                    send_telegram(f"🔥 <b>[{t['type']} RUNNER TARGET HIT]</b> @ ${p:,.2f} | PnL: <b>{pnl_pct:+.2f}%</b> (+3.5R)")
                    continue

            rem.append(t)
        ACTIVE_TRADES = rem

def master_execution_scan():
    global ACTIVE_TRADES, PROCESSED_CANDLES, DAILY_STATS
    with LOCK:
        reset_daily_stats_if_needed()

        # Hard guardrail: already active trade
        if len(ACTIVE_TRADES) >= 1: 
            return
        if DAILY_STATS["trades_count"] >= MAX_DAILY_TRADES:
            return
        if DAILY_STATS["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            if not DAILY_STATS["locked"]:
                DAILY_STATS["locked"] = True
                send_telegram("🛑 <b>RISK DISCIPLINE:</b> 2 consecutive SL hits. Trading locked for the day.")
            return

        c15 = DATA_HUB["klines_15m"]
        p = DATA_HUB["price"]
        pdh = DATA_HUB["pdh"]
        pdl = DATA_HUB["pdl"]

        if len(c15) < 18 or p < 10000: 
            return

        signal = scan_jha_masterclass_setups(c15, pdh, pdl, p)
        if not signal:
            return

        # STRICT DEDUP CHECK
        candle_ts = signal["candle_time"]
        if candle_ts in PROCESSED_CANDLES:
            return

        t_id = f"TRD_{candle_ts}"

        # DB duplicate check
        conn = db()
        exists = conn.execute("SELECT 1 FROM trades WHERE trade_id = ?", (t_id,)).fetchone()
        conn.close()
        if exists:
            PROCESSED_CANDLES.add(candle_ts)
            return

        t = {
            "id": t_id,
            "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "type": signal["category"],
            "tf": "15M",
            "dir": signal["dir"],
            "setup": signal["setup"],
            "entry": round(signal["entry"], 2),
            "sl": round(signal["sl"], 2),
            "tp1": round(signal["tp1"], 2),
            "tp2": round(signal["tp2"], 2),
            "stage": "OPEN"
        }

        ACTIVE_TRADES.append(t)
        PROCESSED_CANDLES.add(candle_ts)
        DAILY_STATS["trades_count"] += 1

        # Instant DB save
        save_or_update_trade(t, status="OPEN")

        send_telegram(
            f"👑 <b>[GAUTAM JHA CONFIRMED SETUP]</b>\n\n"
            f"<b>Direction:</b> {signal['dir']} ({signal['category']})\n"
            f"🔹 <b>Entry:</b> ${signal['entry']:,.2f}\n"
            f"🛑 <b>Stop Loss:</b> ${signal['sl']:,.2f} (Risk: ${signal['risk']:.1f})\n"
            f"🎯 <b>Target 1 (1:2):</b> ${signal['tp1']:,.2f}\n"
            f"🔥 <b>Target 2 (1:3.5):</b> ${signal['tp2']:,.2f}\n\n"
            f"🧠 <b>Trading Logic:</b> {signal['setup']}\n"
            f"📍 <b>Key Levels:</b> PDH: ${pdh:,.1f} | PDL: ${pdl:,.1f}\n"
            f"🛡 <b>Auto Management:</b> Cost-to-cost Breakeven at TP1\n"
            f"📊 <b>Discipline Status:</b> Trade {DAILY_STATS['trades_count']}/{MAX_DAILY_TRADES}"
        )

def master_loop():
    global STARTED_FLAG
    time.sleep(3)
    if not STARTED_FLAG:
        send_telegram("👑 <b>ENGINE DEDUP LOCK ACTIVE</b>\nZero Duplicate Alerts Guaranteed.")
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
            logging.error(f"Loop error: {e}")
            time.sleep(3)

def keep_alive_ping():
    time.sleep(60)
    while True:
        try:
            url = RENDER_EXTERNAL_URL or "http://127.0.0.1:10000/healthz"
            requests.get(url, timeout=5)
        except Exception:
            pass
        time.sleep(300)

threading.Thread(target=master_loop, daemon=True).start()
threading.Thread(target=keep_alive_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
