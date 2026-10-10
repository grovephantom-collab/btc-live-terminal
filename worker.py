"""
Institutional SMC Master Engine (worker.py) - NOISE-IMMUNE EDITION
- Minimum Risk: $120 (Eliminates $30 noise stops)
- Rejection confirmation on FVG retests
- Bulletproof Single-Leader Lock to prevent duplicate Telegram alerts
"""
import os
import time
import uuid
import queue
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from html import escape

import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [MASTER] %(message)s")
app = Flask(__name__)

# ----------------------------------------------------------------- CONFIG
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")
PORT = int(os.environ.get("PORT", 10000))

MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "4"))
ALLOWED_SESSIONS = {s.strip() for s in os.getenv(
    "TRADE_SESSIONS", "ASIAN RANGE,LONDON EXPANSION,NEW YORK TREND,SESSION CLOSE").split(",")}

# Noise Immunity Adjustments
SWEEP_BUFFER = 75.0          # +/- $75 tolerance around PDH/PDL
SL_BUFFER = 45.0             # Extended protection beyond wicks/zones
WICK_MIN = 0.24              # 24% rejection wick minimum
BODY_MAX = 0.60              # exhaustion on trap candle
VOL_MULT = 1.10              # absorption: >= 1.1x avg volume
VOL_LOOKBACK = 10
SPIKE_CAP = 1200.0           # news circuit breaker
RISK_MIN = 120.0             # NO MORE $30 TIGHT STOPS. Minimum $120 space
RISK_MAX = 650.0
BOS_RISK_MIN = 120.0
BOS_RISK_MAX = 550.0
TP1_R, TP2_R = 2.0, 3.5
FVG_MIN_SIZE = 35.0
LOCK_SECONDS = 24 * 3600     # 24hr freeze on 2 consecutive losses
PRICE_MAX_AGE, KLINE_MAX_AGE = 25, 120

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
HTTP = requests.Session()
HTTP.headers.update(HEADERS)

WORKER_ID = uuid.uuid4().hex[:8]
HUB_LOCK = threading.Lock()
DATA_HUB = {"price": 0.0, "price_ts": 0.0, "pdh": 0.0, "pdl": 0.0,
            "session": "NEW YORK TREND", "klines": [], "kline_ts": 0.0}
ACTIVE_POIS = []


# --------------------------------------------------------------------- DB
def db():
    conn = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, created_date TEXT, created_at TEXT, closed_at TEXT,
        signal_type TEXT, tf TEXT, direction TEXT, setup TEXT,
        entry REAL, sl REAL, tp1 REAL, tp2 REAL, exit REAL,
        result TEXT, pnl_r REAL, pnl_percent REAL, status TEXT)""")
    
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
    for name, typ in (("risk", "REAL"), ("closed_ts", "REAL"), ("session", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(created_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS leader (
        id INTEGER PRIMARY KEY CHECK (id = 1), owner TEXT, ts REAL)""")
    conn.close()


init_db()


def acquire_leader(ttl=15.0):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT owner, ts FROM leader WHERE id=1").fetchone()
        now = time.time()
        if row is None or row["owner"] == WORKER_ID or (now - row["ts"]) > ttl:
            conn.execute("INSERT OR REPLACE INTO leader (id, owner, ts) VALUES (1, ?, ?)", (WORKER_ID, now))
            conn.execute("COMMIT")
            return True
        conn.execute("ROLLBACK")
        return False
    except Exception as e:
        try: conn.execute("ROLLBACK")
        except Exception: pass
        logging.warning(f"Leader lock check: {e}")
        return False
    finally:
        conn.close()


# --------------------------------------------------------------- TELEGRAM
TG_QUEUE = queue.Queue(maxsize=100)


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        TG_QUEUE.put_nowait(text)
    except queue.Full:
        logging.warning("Telegram queue full, dropped")


def telegram_worker():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    while True:
        text = TG_QUEUE.get()
        for attempt in range(3):
            try:
                r = HTTP.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                         "parse_mode": "HTML"}, timeout=8)
                if r.status_code == 429:
                    time.sleep(r.json().get("parameters", {}).get("retry_after", 3))
                    continue
                if r.ok:
                    break
            except Exception as e:
                logging.error(f"Telegram worker error: {type(e).__name__}")
            time.sleep(2.0 * (attempt + 1))


# ----------------------------------------------------------------- ROUTES
@app.route("/")
@app.route("/healthz")
def health():
    return "Institutional Master Engine Live & Scanning", 200


def compute_stats(conn):
    rows = conn.execute("SELECT result, pnl_r FROM trades WHERE status='CLOSED'").fetchall()
    n = len(rows)
    wins = sum(1 for r in rows if (r["pnl_r"] or 0) > 0)
    losses = sum(1 for r in rows if (r["pnl_r"] or 0) < 0)
    be = n - wins - losses
    total_r = round(sum((r["pnl_r"] or 0) for r in rows), 2)
    return {"closed": n, "wins": wins, "losses": losses, "breakeven": be,
            "win_rate": round(100 * wins / n, 1) if n else 0.0, "total_r": total_r}


@app.route("/vault-data")
def api_vault():
    try:
        conn = db()
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        active = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN','RISK_FREE')").fetchall()
        stats = compute_stats(conn)
        conn.close()
        with HUB_LOCK:
            snap = {k: DATA_HUB[k] for k in ("price", "pdh", "pdl", "session")}
            pois = list(ACTIVE_POIS)
        resp = jsonify({"vault": [dict(r) for r in rows], "active": [dict(r) for r in active],
                        "pois": pois, "stats": stats, **snap})
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200
    except Exception as e:
        logging.error(f"vault-data error: {e}")
        return jsonify({"vault": [], "active": [], "pois": [], "stats": {}}), 500


# ------------------------------------------------------------ MARKET DATA
def get_session_name():
    hr = datetime.now(timezone.utc).hour
    if 0 <= hr < 7: return "ASIAN RANGE"
    if 7 <= hr < 13: return "LONDON EXPANSION"
    if 13 <= hr < 21: return "NEW YORK TREND"
    return "SESSION CLOSE"


def _get_json(url):
    r = HTTP.get(url, timeout=5)
    r.raise_for_status()
    return r.json()


def fetch_price():
    d = _get_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    price = float(d["data"]["amount"])
    with HUB_LOCK:
        DATA_HUB["price"] = price
        DATA_HUB["price_ts"] = time.time()
        DATA_HUB["session"] = get_session_name()


def fetch_daily():
    rd = _get_json("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400")
    if isinstance(rd, list) and len(rd) >= 2:
        with HUB_LOCK:
            DATA_HUB["pdl"], DATA_HUB["pdh"] = float(rd[1][1]), float(rd[1][2])


def find_fvgs(c, lookback=30):
    out = []
    n = len(c)
    for i in range(max(1, n - lookback), n - 1):
        a, b = c[i - 1], c[i + 1]
        later = c[i + 2:]
        if a["high"] < b["low"] and (b["low"] - a["high"]) >= FVG_MIN_SIZE:
            lo, hi = a["high"], b["low"]
            if all(x["close"] > lo for x in later):
                out.append({"dir": "LONG", "lo": lo, "hi": hi, "idx": i})
        elif a["low"] > b["high"] and (a["low"] - b["high"]) >= FVG_MIN_SIZE:
            lo, hi = b["high"], a["low"]
            if all(x["close"] < hi for x in later):
                out.append({"dir": "SHORT", "lo": lo, "hi": hi, "idx": i})
    return sorted(out, key=lambda f: -f["idx"])


def find_fvg_pois(c):
    pois = []
    for f in find_fvgs(c)[:2]:
        mid = round((f["lo"] + f["hi"]) / 2, 2)
        pois.append({"type": "FVG", "title": f"{'Bull' if f['dir']=='LONG' else 'Bear'} FVG",
                     "price": mid, "color": "#ffd600"})
    return pois


def fetch_klines():
    r15 = _get_json("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900")
    if not (isinstance(r15, list) and len(r15) > 60):
        return
    parsed = [{"time": int(b[0]), "low": float(b[1]), "high": float(b[2]),
               "open": float(b[3]), "close": float(b[4]), "vol": float(b[5])}
              for b in reversed(r15[1:101])]
    ref = parsed[-16:-2]
    with HUB_LOCK:
        DATA_HUB["klines"] = parsed
        DATA_HUB["kline_ts"] = time.time()
        pdh, pdl = DATA_HUB["pdh"], DATA_HUB["pdl"]
        ACTIVE_POIS[:] = [
            {"type": "PDH", "title": "PDH Benchmark", "price": pdh, "color": "#ff1744"},
            {"type": "PDL", "title": "PDL Benchmark", "price": pdl, "color": "#00e676"},
            {"type": "BSL", "title": "Swing High (BSL)", "price": max(x["high"] for x in ref), "color": "#ef5350"},
            {"type": "SSL", "title": "Swing Low (SSL)", "price": min(x["low"] for x in ref), "color": "#26a69a"},
        ]
        ACTIVE_POIS.extend(find_fvg_pois(parsed))


def vol_absorbed(c, idx):
    start = idx - VOL_LOOKBACK
    if start < -len(c): return False
    window = c[start:idx]
    if len(window) < VOL_LOOKBACK: return False
    avg = sum(x["vol"] for x in window) / len(window)
    return avg > 0 and c[idx]["vol"] >= VOL_MULT * avg


def make_signal(direction, cat, setup, p, sl, rmin, rmax, t):
    risk = round(abs(p - sl), 2)
    if (direction == "LONG" and sl >= p) or (direction == "SHORT" and sl <= p):
        return None
    # Reject tight and oversized stops strictly
    if not (rmin <= risk <= rmax):
        return None
    m = 1 if direction == "LONG" else -1
    return {"dir": direction, "cat": cat, "setup": setup, "entry": p, "sl": round(sl, 2),
            "risk": risk, "tp1": round(p + m * TP1_R * risk, 2), "tp2": round(p + m * TP2_R * risk, 2),
            "time": t}


def scan_setups(c15, pdh, pdl, p):
    if len(c15) < 40 or p < 10000: return None

    cur, prev = c15[-1], c15[-2]
    prev_range = max(prev["high"] - prev["low"], 1.0)
    cur_range = max(cur["high"] - cur["low"], 1.0)

    # Circuit Breaker: News volatility
    if prev_range > SPIKE_CAP or cur_range > SPIKE_CAP:
        return None

    body = abs(prev["close"] - prev["open"]) / prev_range
    upper_wick = (prev["high"] - max(prev["open"], prev["close"])) / prev_range
    lower_wick = (min(prev["open"], prev["close"]) - prev["low"]) / prev_range
    absorbed = vol_absorbed(c15, -2)
    ref = c15[-16:-2]
    swing_high = max(x["high"] for x in ref)
    swing_low = min(x["low"] for x in ref)
    t = cur["time"]
    vtag = " + Absorption" if absorbed else ""

    # 1) SSL / PDL Sweep Reversal -> LONG
    pdl_hit = pdl > 0 and prev["low"] <= pdl + SWEEP_BUFFER and prev["close"] > pdl
    ssl_hit = prev["low"] < swing_low and prev["close"] > swing_low
    if ((pdl_hit or ssl_hit) and lower_wick >= WICK_MIN and body <= BODY_MAX
            and cur["close"] > cur["open"] and cur["close"] > prev["close"]):
        cat = "PDL_TRAP" if pdl_hit else "SSL_SWEEP"
        sl_calc = min(prev["low"], cur["low"]) - SL_BUFFER
        sig = make_signal("LONG", cat, f"{cat}: Liquidity Hunt + {lower_wick*100:.0f}% Wick{vtag}",
                          p, sl_calc, RISK_MIN, RISK_MAX, t)
        if sig: return sig

    # 2) BSL / PDH Sweep Reversal -> SHORT
    pdh_hit = pdh > 0 and prev["high"] >= pdh - SWEEP_BUFFER and prev["close"] < pdh
    bsl_hit = prev["high"] > swing_high and prev["close"] < swing_high
    if ((pdh_hit or bsl_hit) and upper_wick >= WICK_MIN and body <= BODY_MAX
            and cur["close"] < cur["open"] and cur["close"] < prev["close"]):
        cat = "PDH_TRAP" if pdh_hit else "BSL_SWEEP"
        sl_calc = max(prev["high"], cur["high"]) + SL_BUFFER
        sig = make_signal("SHORT", cat, f"{cat}: Retail Trap + {upper_wick*100:.0f}% Wick{vtag}",
                          p, sl_calc, RISK_MIN, RISK_MAX, t)
        if sig: return sig

    # 3) BOS Momentum Expansion
    if cur["close"] > swing_high and prev["low"] > swing_low + 150 and cur["close"] > cur["open"]:
        sl_calc = min(cur["low"], prev["low"]) - SL_BUFFER
        sig = make_signal("LONG", "BOS_EXPANSION", "Break of Structure Bullish Continuation",
                          p, sl_calc, BOS_RISK_MIN, BOS_RISK_MAX, t)
        if sig: return sig
    if cur["close"] < swing_low and prev["high"] < swing_high - 150 and cur["close"] < cur["open"]:
        sl_calc = max(cur["high"], prev["high"]) + SL_BUFFER
        sig = make_signal("SHORT", "BOS_EXPANSION", "Break of Structure Bearish Continuation",
                          p, sl_calc, BOS_RISK_MIN, BOS_RISK_MAX, t)
        if sig: return sig

    # 4) FVG Retest WITH STRICT CONFIRMATION (No Blind Touching)
    closes = [x["close"] for x in c15[-50:]]
    trend_up = cur["close"] > (sum(closes) / len(closes))
    cur_wick_low = (min(cur["open"], cur["close"]) - cur["low"]) / cur_range
    cur_wick_high = (cur["high"] - max(cur["open"], cur["close"])) / cur_range

    for f in find_fvgs(c15):
        if f["idx"] >= len(c15) - 3: continue
        # Long retest: Price must dip into zone, bounce, leave a wick >= 20% and close GREEN
        if f["dir"] == "LONG" and trend_up:
            if (cur["low"] <= f["hi"] and cur["close"] >= f["lo"] and cur["close"] > cur["open"]
                    and cur_wick_low >= 0.20):
                # SL sits below the FVG zone floor with buffer, preventing inside-zone stops
                sl_calc = f["lo"] - SL_BUFFER
                sig = make_signal("LONG", "FVG_RETEST", "Bullish FVG Retest with Rejection Confirmation",
                                  p, sl_calc, RISK_MIN, RISK_MAX, t)
                if sig: return sig
        # Short retest: Price must test zone, reject with wick >= 20% and close RED
        elif f["dir"] == "SHORT" and not trend_up:
            if (cur["high"] >= f["lo"] and cur["close"] <= f["hi"] and cur["close"] < cur["open"]
                    and cur_wick_high >= 0.20):
                sl_calc = f["hi"] + SL_BUFFER
                sig = make_signal("SHORT", "FVG_RETEST", "Bearish FVG Retest with Rejection Confirmation",
                                  p, sl_calc, RISK_MIN, RISK_MAX, t)
                if sig: return sig

    return None


# --------------------------------------------------------- RISK ENGINE
def pct(direction, entry, exit_):
    d = (exit_ - entry) if direction == "LONG" else (entry - exit_)
    return round(d / entry * 100, 2)


def trading_locked(conn):
    rows = conn.execute("""SELECT result, closed_ts FROM trades WHERE status='CLOSED'
                           ORDER BY closed_ts DESC, id DESC LIMIT 2""").fetchall()
    if len(rows) < 2: return False
    if all((r["result"] or "").startswith("SL HIT") for r in rows):
        last = rows[0]["closed_ts"] or 0
        return (time.time() - last) < LOCK_SECONDS
    return False


def close_trade(conn, t, exit_price, result, pnl_r):
    res = conn.execute(
        """UPDATE trades SET status='CLOSED', exit=?, result=?, pnl_r=?, pnl_percent=?,
           closed_at=?, closed_ts=? WHERE trade_id=? AND status!='CLOSED'""",
        (exit_price, result, pnl_r, pct(t["direction"], t["entry"], exit_price),
         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), time.time(), t["trade_id"]))
    return res.rowcount > 0


def manage_positions(p):
    conn = db()
    try:
        for t in conn.execute("SELECT * FROM trades WHERE status IN ('OPEN','RISK_FREE')").fetchall():
            long_ = t["direction"] == "LONG"
            entry, sl, tp1, tp2, status = t["entry"], t["sl"], t["tp1"], t["tp2"], t["status"]
            tag = escape(t["signal_type"] or "")

            reached = (lambda lvl: p >= lvl) if long_ else (lambda lvl: p <= lvl)
            stopped = (lambda lvl: p <= lvl) if long_ else (lambda lvl: p >= lvl)

            # TP2 Target
            if reached(tp2):
                if close_trade(conn, t, tp2, f"TP2 HIT (+{TP2_R}R)", TP2_R):
                    send_telegram(f"🔥 <b>[{tag} TP2 TARGET HIT]</b>\nExit: ${tp2:,.2f} | "
                                  f"PnL: <b>{pct(t['direction'], entry, tp2):+.2f}%</b> (+{TP2_R}R)")
                continue

            # TP1 -> Shift Stop Loss to Breakeven
            if status == "OPEN" and reached(tp1):
                r = conn.execute("UPDATE trades SET status='RISK_FREE', sl=? WHERE trade_id=? AND status='OPEN'",
                                 (entry, t["trade_id"]))
                if r.rowcount > 0:
                    send_telegram(f"🎯 <b>[{tag}] TP1 HIT (+{TP1_R}R)</b>\n"
                                  f"Stop Loss shifted to Entry (${entry:,.2f}). Trade is now 100% Risk-Free!")

            # Stop Loss Trigger
            if stopped(sl):
                full_loss = status == "OPEN"
                res_text = "SL HIT (-1R)" if full_loss else "BREAKEVEN EXIT (0R)"
                pnl_r = -1.0 if full_loss else 0.0
                if close_trade(conn, t, sl, res_text, pnl_r):
                    send_telegram(f"🏁 <b>[{tag} CLOSED]</b> {res_text}\n"
                                  f"Exit: ${sl:,.2f} | PnL: <b>{pct(t['direction'], entry, sl):+.2f}%</b>")
                    if full_loss and trading_locked(conn):
                        send_telegram("⛔ <b>Drawdown Lock:</b> 2 consecutive SL hits. Scanning frozen for 24 hours.")
    finally:
        conn.close()


# ------------------------------------------------------------ MAIN SCANNER
def try_signal(p):
    with HUB_LOCK:
        klines = list(DATA_HUB["klines"])
        pdh, pdl, session = DATA_HUB["pdh"], DATA_HUB["pdl"], DATA_HUB["session"]
        fresh = (time.time() - DATA_HUB["price_ts"] < PRICE_MAX_AGE
                 and time.time() - DATA_HUB["kline_ts"] < KLINE_MAX_AGE)
    
    if not fresh or session not in ALLOWED_SESSIONS:
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = db()
    try:
        if trading_locked(conn):
            return
        active = conn.execute("SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','RISK_FREE')").fetchone()[0]
        day_n = conn.execute("SELECT COUNT(*) FROM trades WHERE created_date=?", (today,)).fetchone()[0]
        
        if active > 0 or day_n >= MAX_DAILY_TRADES:
            return

        sig = scan_setups(klines, pdh, pdl, p)
        if sig:
            tid = f"TRD_{sig['time']}_{sig['cat']}"
            t_time = datetime.now(timezone.utc).strftime("%H:%M:%S")
            try:
                conn.execute("""INSERT INTO trades 
                    (trade_id, created_date, created_at, signal_type, tf, direction, setup, entry, sl, tp1, tp2, status) 
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", 
                    (tid, today, t_time, sig["cat"], "15M", sig["dir"], sig["setup"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], "OPEN"))
                
                send_telegram(
                    f"👑 <b>[INSTITUTIONAL SIGNAL CONFIRMED]</b>\n\n"
                    f"<b>Direction: {sig['dir']} ({sig['cat']})</b>\n"
                    f"🔹 <b>Entry:</b> ${sig['entry']:,.2f}\n"
                    f"🛑 <b>Stop Loss:</b> ${sig['sl']:,.2f} (Risk: ${sig['risk']:.1f})\n"
                    f"🎯 <b>TP1 (1:{TP1_R}):</b> ${sig['tp1']:,.2f}\n"
                    f"🔥 <b>TP2 (1:{TP2_R}):</b> ${sig['tp2']:,.2f}\n\n"
                    f"🧠 <b>Trading Logic:</b> {sig['setup']}\n"
                    f"📍 <b>Session:</b> {session}\n"
                    f"🛡️ <b>Risk Engine:</b> Auto-Breakeven at TP1"
                )
            except sqlite3.IntegrityError:
                pass
    finally:
        conn.close()


def master_loop():
    last_daily = 0
    last_kline = 0
    while True:
        try:
            now = time.time()
            if now - last_daily > 300:
                fetch_daily()
                last_daily = now
            if now - last_kline > 15:
                fetch_klines()
                last_kline = now
            
            fetch_price()
            p = DATA_HUB["price"]
            
            if p > 10000 and acquire_leader():
                manage_positions(p)
                try_signal(p)
        except Exception as e:
            logging.error(f"Loop cycle error: {e}")
        time.sleep(3)


# Launch Background Services
threading.Thread(target=telegram_worker, daemon=True).start()
threading.Thread(target=master_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
