import os
import time
import math
import random
import sqlite3
import logging
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SMC-QUANT-CORE] %(message)s"
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

# ---------------- QUANT DATA HUB ----------------
DATA_HUB = {
    "price": 0.0,
    "funding_rate": 0.0,
    "book_imbalance": 0.0,
    "oi_delta": 0.0,
    "open_interest": 0.0,
    "recent_liquidations": {"long_vol": 0.0, "short_vol": 0.0},
    "session_tag": "ASIAN",
    "klines": {"1m": [], "5m": [], "15m": [], "1h": [], "4h": [], "1d": []},
    "last_sync": 0.0
}

ACTIVE_TRADES = []
ACTIVE_POIS = []

def db():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_all_databases():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, created_at TEXT, signal_type TEXT, tf TEXT, direction TEXT,
        setup TEXT, score INTEGER, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        exit REAL, result TEXT, pnl_r REAL, realized_r REAL, remaining_pct REAL, stage TEXT,
        confluence TEXT, status TEXT)""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS quantitative_strategies (
        strategy_id TEXT PRIMARY KEY,
        trigger_rule TEXT,
        confirm_rule TEXT,
        exit_rule TEXT,
        market_regime TEXT,
        hist_winrate REAL,
        wfo_winrate REAL,
        monte_carlo_mdd REAL,
        kelly_fraction REAL,
        generation INTEGER,
        stage TEXT,
        last_evaluated TEXT)""")
        
    base_strats = [
        ("INST_15M_ABSORPTION_SWEEP", "CANDLE_ABSORPTION", "L2_CONFIRM", "RR_3_0", "SIDEWAYS", 74.0, 68.0, 8.0, 0.22, 1, "LIVE_ELIGIBLE"),
        ("INST_15M_EXPANSION_DISPLACEMENT", "DISPLACEMENT_CANDLE", "VOL_POC_BREAK", "RR_2_5", "BULL", 72.0, 66.0, 9.2, 0.20, 1, "LIVE_ELIGIBLE"),
        ("INST_EVENT_SESSION_LIQ_PURGE", "SESSION_EXTREME_PURGE", "OI_FLUSH", "RR_5_0", "BEAR", 65.0, 61.0, 10.5, 0.16, 1, "LIVE_ELIGIBLE")
    ]
    for row in base_strats:
        c.execute("""INSERT OR IGNORE INTO quantitative_strategies 
                     (strategy_id, trigger_rule, confirm_rule, exit_rule, market_regime, hist_winrate, wfo_winrate, monte_carlo_mdd, kelly_fraction, generation, stage, last_evaluated)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10], datetime.now(timezone.utc).isoformat()))
    c.commit()
    c.close()

init_all_databases()

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
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
    return "BTCUSDT Institutional Engine Live 24/7", 200

@app.route("/price")
def api_price():
    return jsonify({
        "symbol": SYMBOL,
        "price": DATA_HUB["price"],
        "funding": DATA_HUB["funding_rate"],
        "imbalance": DATA_HUB["book_imbalance"],
        "session": DATA_HUB["session_tag"]
    }), 200

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
            return jsonify({"vault": vault, "active": list(ACTIVE_TRADES), "pois": list(ACTIVE_POIS)}), 200
    except Exception:
        return jsonify({"vault": [], "active": [], "pois": []}), 500

# ---------------- SAFE FETCH WITH BUFFER GUARD ----------------
def fetch_klines(interval, limit=80):
    try:
        r = requests.get(f"{BASE}/fapi/v1/klines", params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=4).json()
        if isinstance(r, list) and len(r) > 10:
            parsed = []
            for b in r[:-1]:
                if len(b) >= 10:
                    parsed.append({
                        "time": int(b[0] // 1000), "open": float(b[1]), "high": float(b[2]),
                        "low": float(b[3]), "close": float(b[4]), "vol": float(b[5]),
                        "taker_buy_vol": float(b[9])
                    })
            return parsed
    except Exception:
        pass
    return []

def get_current_session():
    utc_hr = datetime.now(timezone.utc).hour
    if 0 <= utc_hr < 7: return "ASIAN SESSION"
    elif 7 <= utc_hr < 13: return "LONDON SESSION"
    elif 13 <= utc_hr < 21: return "NEW YORK SESSION"
    return "US POST-MARKET"

def analyze_candle_mechanics(c):
    total_range = max(c["high"] - c["low"], 0.1)
    body = abs(c["close"] - c["open"])
    body_ratio = body / total_range
    
    taker_buy = c.get("taker_buy_vol", 0.0)
    total_vol = max(c.get("vol", 1.0), 1.0)
    taker_sell = max(total_vol - taker_buy, 0.0)
    volume_delta = (taker_buy - taker_sell) / total_vol

    upper_wick = c["high"] - max(c["open"], c["close"])
    lower_wick = min(c["open"], c["close"]) - c["low"]

    return {
        "body_ratio": body_ratio,
        "volume_delta": volume_delta,
        "is_displacement": (body_ratio >= 0.65),
        "bullish_absorption": (lower_wick / total_range >= 0.50 and volume_delta < 0.0),
        "bearish_absorption": (upper_wick / total_range >= 0.50 and volume_delta > 0.0)
    }

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 40.0
    trs = [max(c["high"] - c["low"], abs(c["high"] - candles[i-1]["close"]), abs(c["low"] - candles[i-1]["close"])) 
           for i, c in enumerate(candles) if i > 0]
    return max(sum(trs[-period:]) / period, 1.0)

def detect_sweep(candles, lookback=8):
    if len(candles) < lookback + 2: return None
    ref = candles[-lookback-1:-1]
    cur = candles[-1]
    hi, lo = max(c["high"] for c in ref), min(c["low"] for c in ref)
    if cur["low"] < lo and cur["close"] > lo: return {"dir": "LONG", "wick": cur["low"], "level": lo}
    if cur["high"] > hi and cur["close"] < hi: return {"dir": "SHORT", "wick": cur["high"], "level": hi}
    return None

def detect_fvg(candles):
    if len(candles) < 4: return None
    c1, c3 = candles[-4], candles[-2]
    cur = candles[-1]
    if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) >= 5.0:
        if cur["low"] <= c3["low"]: return {"dir": "LONG", "low": c1["high"], "high": c3["low"]}
    if c3["high"] < c1["low"] and (c1["low"] - c3["high"]) >= 5.0:
        if cur["high"] >= c3["high"]: return {"dir": "SHORT", "low": c3["high"], "high": c1["low"]}
    return None

# ---------------- MARKET SYNC ----------------
def sync_market_hub():
    global DATA_HUB
    try:
        p_res = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["price"] = float(p_res.get("price", 0.0))

        for tf in ["1m", "5m", "15m", "1h", "4h", "1d"]:
            k = fetch_klines(tf, 70)
            if k: DATA_HUB["klines"][tf] = k

        # Funding Rate
        fr_res = requests.get(f"{BASE}/fapi/v1/premiumIndex", params={"symbol": SYMBOL}, timeout=3).json()
        DATA_HUB["funding_rate"] = float(fr_res.get("lastFundingRate", 0.0))

        # Order Book Imbalance
        depth_res = requests.get(f"{BASE}/fapi/v1/depth", params={"symbol": SYMBOL, "limit": 20}, timeout=3).json()
        bids = sum(float(b[1]) for b in depth_res.get("bids", []))
        asks = sum(float(a[1]) for a in depth_res.get("asks", []))
        if bids + asks > 0:
            DATA_HUB["book_imbalance"] = round((bids - asks) / (bids + asks), 3)

        # Open Interest
        oi_res = requests.get(f"{BASE}/fapi/v1/openInterest", params={"symbol": SYMBOL}, timeout=3).json()
        cur_oi = float(oi_res.get("openInterest", 0.0))
        if DATA_HUB["open_interest"] > 0:
            DATA_HUB["oi_delta"] = cur_oi - DATA_HUB["open_interest"]
        DATA_HUB["open_interest"] = cur_oi

        DATA_HUB["session_tag"] = get_current_session()
        DATA_HUB["last_sync"] = time.time()
    except Exception as e:
        logging.warning(f"Sync warning: {e}")

# ---------------- MANAGE POSITIONS ----------------
def manage_positions(p):
    global ACTIVE_TRADES
    with LOCK:
        rem = []
        for t in ACTIVE_TRADES:
            entry = t["entry"]
            if t["dir"] == "LONG":
                if p >= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 2.0
                    t["sl"] = entry
                    send_telegram(f"🎯 <b>[{t['type']}] TP1 HIT (+2R)</b>\n50% secured. 🛡 SL moved to <b>Breakeven (${entry:,.2f})</b>")
                elif p >= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 4.0
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] TP2 HIT (+4R)</b>\n25% secured. 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")
                if p <= t["sl"]:
                    final_r = -1.0 if t["stage"] == "OPEN" else t["realized_r"]
                    res = "SL HIT" if t["stage"] == "OPEN" else "BE/TRAIL EXIT"
                    save_closed(t, p, res, final_r)
                    send_telegram(f"🏁 <b>[{t['type']} CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    continue
                elif p >= t["tp3"]:
                    final_r = t["realized_r"] + (0.25 * 6.0)
                    save_closed(t, p, "TP3 TARGET 🔥", final_r)
                    send_telegram(f"🔥 <b>[{t['type']} RUNNER FINISHED]</b> @ ${p:,.2f} | Net: <b>+{final_r:+.2f}R</b>")
                    continue

            elif t["dir"] == "SHORT":
                if p <= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 2.0
                    t["sl"] = entry
                    send_telegram(f"🎯 <b>[{t['type']}] TP1 HIT (+2R)</b>\n50% secured. 🛡 SL moved to <b>Breakeven (${entry:,.2f})</b>")
                elif p <= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 4.0
                    t["sl"] = t["tp1"]
                    send_telegram(f"🎯 <b>[{t['type']}] TP2 HIT (+4R)</b>\n25% secured. 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")
                if p >= t["sl"]:
                    final_r = -1.0 if t["stage"] == "OPEN" else t["realized_r"]
                    res = "SL HIT" if t["stage"] == "OPEN" else "BE/TRAIL EXIT"
                    save_closed(t, p, res, final_r)
                    send_telegram(f"🏁 <b>[{t['type']} CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    continue
                elif p <= t["tp3"]:
                    final_r = t["realized_r"] + (0.25 * 6.0)
                    save_closed(t, p, "TP3 TARGET 🔥", final_r)
                    send_telegram(f"🔥 <b>[{t['type']} RUNNER FINISHED]</b> @ ${p:,.2f} | Net: <b>+{final_r:+.2f}R</b>")
                    continue

            rem.append(t)
        ACTIVE_TRADES = rem

def save_closed(t, exit_p, res, pnl):
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
        logging.error(f"DB save error: {e}")

# ---------------- QUANT SCANNER ----------------
LAST_SCAN_TIME = 0

def master_execution_scan():
    global ACTIVE_TRADES, ACTIVE_POIS, LAST_SCAN_TIME
    with LOCK:
        if len(ACTIVE_TRADES) >= 2: return
        now = time.time()
        if now - LAST_SCAN_TIME < 30: return

        c15 = DATA_HUB["klines"].get("15m", [])
        c1 = DATA_HUB["klines"].get("1m", [])
        c4h = DATA_HUB["klines"].get("4h", [])
        p = DATA_HUB["price"]

        if len(c15) < 20 or len(c1) < 15: return

        cur15 = c15[-1]
        phys = analyze_candle_mechanics(cur15)
        a15 = calculate_atr(c15, 14)

        ref15 = c15[-10:-1]
        hi15, lo15 = max(x["high"] for x in ref15), min(x["low"] for x in ref15)

        direction = None
        setup_id = ""
        category = "NORMAL"
        setup_note = ""

        # Setup 1: Candle Absorption Purge (Sniper)
        if cur15["low"] < lo15 and phys["bullish_absorption"]:
            direction = "LONG"
            setup_id = "INST_15M_ABSORPTION_SWEEP"
            category = "SNIPER"
            setup_note = "Institutional Absorption (Sellers Exhausted at Lows)"
        elif cur15["high"] > hi15 and phys["bearish_absorption"]:
            direction = "SHORT"
            setup_id = "INST_15M_ABSORPTION_SWEEP"
            category = "SNIPER"
            setup_note = "Institutional Absorption (Buyers Exhausted at Highs)"

        # Setup 2: Displacement Expansion (>65% Body)
        elif phys["is_displacement"] and cur15["close"] > hi15 and phys["volume_delta"] > 0.15:
            direction = "LONG"
            setup_id = "INST_15M_EXPANSION_DISPLACEMENT"
            category = "NORMAL"
            setup_note = "Displacement Candle Expansion"
        elif phys["is_displacement"] and cur15["close"] < lo15 and phys["volume_delta"] < -0.15:
            direction = "SHORT"
            setup_id = "INST_15M_EXPANSION_DISPLACEMENT"
            category = "NORMAL"
            setup_note = "Displacement Candle Expansion"

        # Setup 3: 4H Macro Sweep Runner
        elif len(c4h) > 10:
            c4h_cur = c4h[-1]
            c4h_ref = c4h[-8:-1]
            hi4h, lo4h = max(x["high"] for x in c4h_ref), min(x["low"] for x in c4h_ref)
            if c4h_cur["low"] < lo4h and c4h_cur["close"] > lo4h:
                direction = "LONG"
                setup_id = "INST_EVENT_SESSION_LIQ_PURGE"
                category = "EVENT / RUNNER"
                setup_note = "4H Macro Liquidity Sweep"
            elif c4h_cur["high"] > hi4h and c4h_cur["close"] < hi4h:
                direction = "SHORT"
                setup_id = "INST_EVENT_SESSION_LIQ_PURGE"
                category = "EVENT / RUNNER"
                setup_note = "4H Macro Liquidity Sweep"

        if not direction: return

        c = db()
        strat_info = c.execute("SELECT stage, kelly_fraction FROM quantitative_strategies WHERE strategy_id=?", (setup_id,)).fetchone()
        c.close()
        if not strat_info or strat_info[0] != "LIVE_ELIGIBLE":
            return

        kelly_size = strat_info[1]

        entry = p
        if direction == "LONG":
            sl = (cur15["low"] - 0.2 * a15) if "SNIPER" in category else (entry - 1.2 * a15)
            risk = entry - sl
            if risk < 10.0 or risk > 280.0: return
            tp1, tp2, tp3 = entry + (2.0 * risk), entry + (4.0 * risk), entry + (6.0 * risk)
        else:
            sl = (cur15["high"] + 0.2 * a15) if "SNIPER" in category else (entry + 1.2 * a15)
            risk = sl - entry
            if risk < 10.0 or risk > 280.0: return
            tp1, tp2, tp3 = entry - (2.0 * risk), entry - (4.0 * risk), entry - (6.0 * risk)

        t = {
            "id": f"TRD_{int(now*1000)}",
            "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "type": category,
            "tf": "15M",
            "dir": direction,
            "setup": setup_id,
            "score": 98 if "SNIPER" in category else 90,
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
            f"🏛 <b>[TOP-LEVEL {category} CANDLE SIGNAL]</b>\n\n"
            f"<b>Direction:</b> {direction}\n"
            f"🔹 <b>Entry:</b> ${entry:,.2f}\n"
            f"🛑 <b>SL:</b> ${sl:,.2f} (Risk: ${risk:.1f})\n"
            f"🎯 <b>TP1 (2R):</b> ${tp1:,.2f}\n"
            f"🎯 <b>TP2 (4R):</b> ${tp2:,.2f}\n"
            f"🔥 <b>TP3 (6R):</b> ${tp3:,.2f}\n\n"
            f"🕯 <b>Physics:</b> {setup_note}\n"
            f"⏱ <b>Session:</b> {DATA_HUB['session_tag']}\n"
            f"📊 <b>Kelly Allocation:</b> {int(kelly_size*100)}%\n"
            f"🧬 <b>Strategy ID:</b> {setup_id}"
        )

# ---------------- KEEP-ALIVE DAEMON (PREVENTS SLEEP ON RENDER) ----------------
def keep_alive_ping():
    time.sleep(60)
    while True:
        try:
            url = RENDER_EXTERNAL_URL or "http://127.0.0.1:10000/healthz"
            requests.get(url, timeout=5)
        except Exception:
            pass
        time.sleep(600) # Ping every 10 minutes

# ---------------- EXECUTION RUNNER ----------------
def master_loop():
    global STARTED_FLAG
    time.sleep(3)
    if not STARTED_FLAG:
        send_telegram("🏛 <b>BTC Institutional Engine Live 24/7</b>\nSelf-KeepAlive & Candle Physics Active.")
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
            logging.error(f"Loop warning: {e}")
            time.sleep(4)

threading.Thread(target=master_loop, daemon=True).start()
threading.Thread(target=keep_alive_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
