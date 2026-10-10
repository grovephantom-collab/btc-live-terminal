#!/usr/bin/env python3
"""
INSTITUTIONAL BTC/USDT ENGINE (worker.py)
Filtered by High-Rating Concepts (>5/10):
- Liquidity Sweep & Judas Traps (10/10)
- Pure Price Action & Wick Rejections (10/10)
- Volume Profile (Point of Control - POC) (9/10)
- SMC / ICT Framework (8/10)
- Supply & Demand Order Blocks (8/10)
- Fair Value Gap (FVG) Mitigation (7/10)
- 20/50 Dynamic EMA Trend Filter (7/10)
- Fibonacci Equilibrium / Discount Zone (6/10)
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
from typing import Dict, List, Optional, Any

import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [PRO-SMC] %(message)s")
app = Flask(__name__)

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
DB_FILE = os.getenv("DB_FILE", "trades_v9.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")
PORT = int(os.environ.get("PORT", 10000))

MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "4"))
ALLOWED_SESSIONS = {
    s.strip() for s in os.getenv("TRADE_SESSIONS", "LONDON EXPANSION,NEW YORK TREND,SESSION CLOSE,ASIAN RANGE").split(",")
}

# Institutional Parameters
SWEEP_TOLERANCE = 85.0
SL_BUFFER = 85.0
WICK_MIN_RATIO = 0.25
BODY_MAX_RATIO = 0.55
VOL_ABSORB_MULT = 1.15
VOL_LOOKBACK = 10
NEWS_SPIKE_CAP = 1200.0

# Risk Management
RISK_MIN = 150.0
RISK_MAX = 700.0
TP1_RR = 2.0
TP2_RR = 3.5
FVG_MIN_SIZE = 40.0
LOCK_SECONDS = 24 * 3600
DATA_MAX_AGE_SEC = 25

HEADERS = {"User-Agent": "Pro-SMC-Engine/8.0", "Accept": "application/json"}
HTTP = requests.Session()
HTTP.headers.update(HEADERS)

WORKER_ID = uuid.uuid4().hex[:8]
STATE_LOCK = threading.Lock()
DATA_HUB: Dict[str, Any] = {
    "price": 0.0,
    "price_ts": 0.0,
    "pdh": 0.0,
    "pdl": 0.0,
    "poc": 0.0,
    "session": "INITIALIZING",
    "klines": [],
    "kline_ts": 0.0
}
ACTIVE_POIS: List[Dict[str, Any]] = []

# ==========================================
# DATABASE & SINGLE-LEADER LOCK
# ==========================================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, created_date TEXT, created_at TEXT, closed_at TEXT,
        signal_type TEXT, tf TEXT, direction TEXT, setup TEXT,
        entry REAL, sl REAL, tp1 REAL, tp2 REAL, exit REAL,
        result TEXT, pnl_r REAL, pnl_percent REAL, status TEXT,
        risk REAL, closed_ts REAL, session TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(created_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS leader (
        id INTEGER PRIMARY KEY CHECK (id = 1), owner TEXT, ts REAL)""")
    conn.close()


init_db()


def acquire_leader(ttl: float = 12.0) -> bool:
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
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        logging.warning(f"Leader lock check: {e}")
        return False
    finally:
        conn.close()


# ==========================================
# ASYNC TELEGRAM DISPATCHER
# ==========================================
TG_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=100)


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        TG_QUEUE.put_nowait(text)
    except queue.Full:
        logging.warning("Telegram queue saturated")


def telegram_worker_loop() -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    while True:
        text = TG_QUEUE.get()
        for attempt in range(4):
            try:
                r = HTTP.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                         "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
                if r.status_code == 429:
                    sleep_dur = r.json().get("parameters", {}).get("retry_after", 4)
                    time.sleep(sleep_dur)
                    continue
                if r.ok:
                    break
            except Exception as e:
                logging.error(f"Telegram worker error: {type(e).__name__}")
            time.sleep(2.0 * (attempt + 1))


# ==========================================
# HTTP ENDPOINTS
# ==========================================
@app.route("/")
@app.route("/healthz")
def healthz():
    return "Institutional Master Engine (>5 Rating Core) Live & Scanning", 200


def compute_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute("SELECT result, pnl_r FROM trades WHERE status='CLOSED'").fetchall()
    n = len(rows)
    wins = sum(1 for r in rows if (r["pnl_r"] or 0) > 0)
    losses = sum(1 for r in rows if (r["pnl_r"] or 0) < 0)
    be = n - wins - losses
    total_r = round(sum((r["pnl_r"] or 0) for r in rows), 2)
    return {
        "closed": n,
        "wins": wins,
        "losses": losses,
        "breakeven": be,
        "win_rate": round(100 * wins / n, 1) if n else 0.0,
        "total_r": total_r,
    }


@app.route("/vault-data")
def vault_data():
    try:
        conn = db()
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        active = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN','RISK_FREE')").fetchall()
        stats = compute_stats(conn)
        conn.close()

        with STATE_LOCK:
            snap = {k: DATA_HUB[k] for k in ("price", "pdh", "pdl", "poc", "session")}
            pois = list(ACTIVE_POIS)

        resp = jsonify({
            "vault": [dict(r) for r in rows],
            "active": [dict(r) for r in active],
            "pois": pois,
            "stats": stats,
            **snap,
        })
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200
    except Exception as e:
        logging.error(f"Vault error: {e}")
        return jsonify({"vault": [], "active": [], "pois": [], "stats": {}}), 500


# ==========================================
# TECHNICAL CALCULATION HELPERS
# ==========================================
def calculate_ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return values[-1] if values else 0.0
    multiplier = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema


def calculate_volume_profile_poc(candles: List[Dict[str, float]], bins_count: int = 25) -> float:
    """9/10 Rating: Volume Profile Point of Control (Highest Volume Price Level)"""
    if not candles:
        return 0.0
    min_p = min(c["low"] for c in candles)
    max_p = max(c["high"] for c in candles)
    if max_p <= min_p:
        return min_p

    bin_width = (max_p - min_p) / bins_count
    volume_by_bin = [0.0] * bins_count

    for c in candles:
        typical_p = (c["high"] + c["low"] + c["close"]) / 3.0
        bin_idx = min(int((typical_p - min_p) / bin_width), bins_count - 1)
        volume_by_bin[bin_idx] += c["vol"]

    max_vol_bin = volume_by_bin.index(max(volume_by_bin))
    poc_price = min_p + (max_vol_bin + 0.5) * bin_width
    return round(poc_price, 2)


# ==========================================
# COINBASE INGESTION
# ==========================================
def get_session_name() -> str:
    hr = datetime.now(timezone.utc).hour
    if 0 <= hr < 7:
        return "ASIAN RANGE"
    if 7 <= hr < 13:
        return "LONDON EXPANSION"
    if 13 <= hr < 21:
        return "NEW YORK TREND"
    return "SESSION CLOSE"


def fetch_spot_price() -> None:
    r = HTTP.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
    r.raise_for_status()
    price = float(r.json()["data"]["amount"])
    with STATE_LOCK:
        DATA_HUB["price"] = price
        DATA_HUB["price_ts"] = time.time()
        DATA_HUB["session"] = get_session_name()


def fetch_daily_pdh_pdl() -> None:
    r = HTTP.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400", timeout=8)
    r.raise_for_status()
    rd = r.json()
    if isinstance(rd, list) and len(rd) >= 2:
        with STATE_LOCK:
            DATA_HUB["pdl"] = float(rd[1][1])
            DATA_HUB["pdh"] = float(rd[1][2])


def find_fvgs(candles: List[Dict[str, float]], lookback: int = 30) -> List[Dict[str, Any]]:
    out = []
    n = len(candles)
    for i in range(max(1, n - lookback), n - 1):
        a, b = candles[i - 1], candles[i + 1]
        later = candles[i + 2:]
        if a["high"] < b["low"] and (b["low"] - a["high"]) >= FVG_MIN_SIZE:
            lo, hi = a["high"], b["low"]
            if all(x["close"] > lo for x in later):
                out.append({"dir": "LONG", "lo": lo, "hi": hi, "idx": i})
        elif a["low"] > b["high"] and (a["low"] - b["high"]) >= FVG_MIN_SIZE:
            lo, hi = b["high"], a["low"]
            if all(x["close"] < hi for x in later):
                out.append({"dir": "SHORT", "lo": lo, "hi": hi, "idx": i})
    return sorted(out, key=lambda f: -f["idx"])


def fetch_15m_klines() -> None:
    r = HTTP.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900", timeout=8)
    r.raise_for_status()
    raw = r.json()
    if not (isinstance(raw, list) and len(raw) > 50):
        return

    parsed = [
        {"time": int(b[0]), "low": float(b[1]), "high": float(b[2]),
         "open": float(b[3]), "close": float(b[4]), "vol": float(b[5])}
        for b in reversed(raw[1:101])
    ]
    ref = parsed[-16:-2]
    poc = calculate_volume_profile_poc(parsed[-40:])

    with STATE_LOCK:
        DATA_HUB["klines"] = parsed
        DATA_HUB["kline_ts"] = time.time()
        DATA_HUB["poc"] = poc
        pdh, pdl = DATA_HUB["pdh"], DATA_HUB["pdl"]
        swing_hi = max(x["high"] for x in ref)
        swing_lo = min(x["low"] for x in ref)
        ACTIVE_POIS[:] = [
            {"type": "PDH", "title": "PDH Liquidity Target", "price": pdh, "color": "#ff1744"},
            {"type": "PDL", "title": "PDL Liquidity Target", "price": pdl, "color": "#00e676"},
            {"type": "POC", "title": "Volume Profile POC (9/10)", "price": poc, "color": "#ff9100"},
            {"type": "BSL", "title": "Swing High (BSL)", "price": swing_hi, "color": "#ef5350"},
            {"type": "SSL", "title": "Swing Low (SSL)", "price": swing_lo, "color": "#26a69a"},
        ]
        for f in find_fvgs(parsed)[:2]:
            mid = round((f["lo"] + f["hi"]) / 2, 2)
            ACTIVE_POIS.append({"type": "FVG", "title": f"{'Bull' if f['dir']=='LONG' else 'Bear'} FVG (7/10)",
                                "price": mid, "color": "#ffd600"})


# ==========================================
# CORE SIGNAL ENGINE (>5 RATED INTEGRATION)
# ==========================================
def volume_absorbed(candles: List[Dict[str, float]], idx: int) -> bool:
    start = idx - VOL_LOOKBACK
    if start < -len(candles):
        return False
    window = candles[start:idx]
    if len(window) < VOL_LOOKBACK:
        return False
    avg = sum(x["vol"] for x in window) / len(window)
    return avg > 0 and candles[idx]["vol"] >= (VOL_ABSORB_MULT * avg)


def build_signal(direction: str, cat: str, setup: str, p: float, sl: float, t: int) -> Optional[Dict[str, Any]]:
    risk = round(abs(p - sl), 2)
    if (direction == "LONG" and sl >= p) or (direction == "SHORT" and sl <= p):
        return None
    if not (RISK_MIN <= risk <= RISK_MAX):
        return None

    mult = 1 if direction == "LONG" else -1
    return {
        "dir": direction,
        "cat": cat,
        "setup": setup,
        "entry": p,
        "sl": round(sl, 2),
        "risk": risk,
        "tp1": round(p + mult * TP1_RR * risk, 2),
        "tp2": round(p + mult * TP2_RR * risk, 2),
        "time": t,
    }


def scan_top_rated_engine(candles: List[Dict[str, float]], pdh: float, pdl: float, p: float, session: str) -> Optional[Dict[str, Any]]:
    if len(candles) < 40 or p < 10000:
        return None

    cur = candles[-1]
    prev = candles[-2]
    prev_range = max(prev["high"] - prev["low"], 1.0)
    cur_range = max(cur["high"] - cur["low"], 1.0)

    if prev_range > NEWS_SPIKE_CAP or cur_range > NEWS_SPIKE_CAP:
        return None

    prev_body = abs(prev["close"] - prev["open"]) / prev_range
    upper_wick = (prev["high"] - max(prev["open"], prev["close"])) / prev_range
    lower_wick = (min(prev["open"], prev["close"]) - prev["low"]) / prev_range
    is_absorbed = volume_absorbed(candles, -2)

    # 1. 20 & 50 EMA Dynamic Filter (7/10)
    close_series = [c["close"] for c in candles]
    ema20 = calculate_ema(close_series, 20)
    ema50 = calculate_ema(close_series, 50)
    ema_bullish = p > ema20 >= ema50
    ema_bearish = p < ema20 <= ema50

    # 2. Fibonacci 50-61.8% Equilibrium / Discount Zone (6/10)
    ref = candles[-20:-2]
    swing_high = max(x["high"] for x in ref)
    swing_low = min(x["low"] for x in ref)
    range_high = max(pdh, swing_high) if pdh > 0 else swing_high
    range_low = min(pdl, swing_low) if pdl > 0 else swing_low
    equilibrium = (range_high + range_low) / 2.0
    in_discount = p < equilibrium
    in_premium = p > equilibrium

    # 3. Volume Profile POC (9/10)
    poc = calculate_volume_profile_poc(candles[-40:])
    t = cur["time"]
    vol_text = " + Vol Absorption" if is_absorbed else ""

    # -------------------------------------------------------------
    # SETUP A: LIQUIDITY SWEEP & JUDAS TRAP (10/10)
    # -------------------------------------------------------------
    pdl_sweep = (pdl > 0) and (prev["low"] <= pdl + SWEEP_TOLERANCE) and (prev["close"] > pdl)
    ssl_sweep = (prev["low"] < swing_low) and (prev["close"] > swing_low)

    if in_discount and (pdl_sweep or ssl_sweep):
        if (lower_wick >= WICK_MIN_RATIO and prev_body <= BODY_MAX_RATIO
                and cur["close"] > cur["open"] and cur["close"] > prev["close"]):
            cat = "JUDAS_TRAP_LONG" if "LONDON" in session or "NEW YORK" in session else "LIQUIDITY_SWEEP_LONG"
            desc = f"{cat}: 10/10 Sweep in Discount ({lower_wick*100:.0f}% Wick{vol_text})"
            sl = min(prev["low"], cur["low"]) - SL_BUFFER
            sig = build_signal("LONG", cat, desc, p, sl, t)
            if sig:
                return sig

    pdh_sweep = (pdh > 0) and (prev["high"] >= pdh - SWEEP_TOLERANCE) and (prev["close"] < pdh)
    bsl_sweep = (prev["high"] > swing_high) and (prev["close"] < swing_high)

    if in_premium and (pdh_sweep or bsl_sweep):
        if (upper_wick >= WICK_MIN_RATIO and prev_body <= BODY_MAX_RATIO
                and cur["close"] < cur["open"] and cur["close"] < prev["close"]):
            cat = "JUDAS_TRAP_SHORT" if "LONDON" in session or "NEW YORK" in session else "LIQUIDITY_SWEEP_SHORT"
            desc = f"{cat}: 10/10 Sweep in Premium ({upper_wick*100:.0f}% Wick{vol_text})"
            sl = max(prev["high"], cur["high"]) + SL_BUFFER
            sig = build_signal("SHORT", cat, desc, p, sl, t)
            if sig:
                return sig

    # -------------------------------------------------------------
    # SETUP B: SUPPLY & DEMAND ORDER BLOCK + POC CONFLUENCE (8/10 & 9/10)
    # -------------------------------------------------------------
    for i in range(len(candles) - 10, len(candles) - 3):
        ob = candles[i]
        nxt = candles[i + 1]
        # Bullish Demand OB (Red candle before explosive green move)
        if ob["close"] < ob["open"] and nxt["close"] > ob["high"]:
            ob_top = ob["open"]
            ob_bot = ob["low"]
            # Price taps Demand in Discount, supported near POC or EMA20
            if in_discount and cur["low"] <= ob_top and cur["close"] >= ob_bot and cur["close"] > cur["open"]:
                if abs(p - poc) <= 250.0 or p >= ema20:
                    sl = ob_bot - SL_BUFFER
                    sig = build_signal("LONG", "DEMAND_OB_POC",
                                       f"Demand Zone (8/10) + POC Retest (${poc:,.0f}) in Discount", p, sl, t)
                    if sig:
                        return sig

        # Bearish Supply OB (Green candle before impulsive dump)
        if ob["close"] > ob["open"] and nxt["close"] < ob["low"]:
            ob_top = ob["high"]
            ob_bot = ob["open"]
            # Price taps Supply in Premium, resisted near POC or EMA20
            if in_premium and cur["high"] >= ob_bot and cur["close"] <= ob_top and cur["close"] < cur["open"]:
                if abs(p - poc) <= 250.0 or p <= ema20:
                    sl = ob_top + SL_BUFFER
                    sig = build_signal("SHORT", "SUPPLY_OB_POC",
                                       f"Supply Zone (8/10) + POC Retest (${poc:,.0f}) in Premium", p, sl, t)
                    if sig:
                        return sig

    # -------------------------------------------------------------
    # SETUP C: FAIR VALUE GAP (FVG) + EMA ALIGNMENT (7/10)
    # -------------------------------------------------------------
    cur_lower_wick = (min(cur["open"], cur["close"]) - cur["low"]) / cur_range
    cur_upper_wick = (cur["high"] - max(cur["open"], cur["close"])) / cur_range

    for fvg in find_fvgs(candles):
        if fvg["idx"] >= len(candles) - 3:
            continue
        # Bullish FVG: In Discount + Trend Bullish (Above EMA50) + Rejection Wick
        if fvg["dir"] == "LONG" and ema_bullish and in_discount:
            if cur["low"] <= fvg["hi"] and cur["close"] >= fvg["lo"] and cur["close"] > cur["open"] and cur_lower_wick >= 0.25:
                sl = fvg["lo"] - SL_BUFFER
                sig = build_signal("LONG", "FVG_EMA_CONFLUENCE",
                                   "Bullish FVG (7/10) with 20/50 EMA Trend & 25%+ Wick", p, sl, t)
                if sig:
                    return sig

        # Bearish FVG: In Premium + Trend Bearish (Below EMA50) + Rejection Wick
        elif fvg["dir"] == "SHORT" and ema_bearish and in_premium:
            if cur["high"] >= fvg["lo"] and cur["close"] <= fvg["hi"] and cur["close"] < cur["open"] and cur_upper_wick >= 0.25:
                sl = fvg["hi"] + SL_BUFFER
                sig = build_signal("SHORT", "FVG_EMA_CONFLUENCE",
                                   "Bearish FVG (7/10) with 20/50 EMA Trend & 25%+ Wick", p, sl, t)
                if sig:
                    return sig

    return None


# ==========================================
# POSITION & RISK EXECUTION
# ==========================================
def pnl_percentage(direction: str, entry: float, exit_price: float) -> float:
    diff = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
    return round((diff / entry) * 100.0, 2)


def is_account_locked(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """SELECT result, closed_ts FROM trades WHERE status='CLOSED'
           ORDER BY closed_ts DESC, id DESC LIMIT 2"""
    ).fetchall()
    if len(rows) < 2:
        return False
    if all((r["result"] or "").startswith("SL HIT") for r in rows):
        last_ts = rows[0]["closed_ts"] or 0
        return (time.time() - last_ts) < LOCK_SECONDS
    return False


def close_trade_record(conn: sqlite3.Connection, trade: sqlite3.Row, exit_p: float, result: str, pnl_r: float) -> bool:
    pct = pnl_percentage(trade["direction"], trade["entry"], exit_p)
    res = conn.execute(
        """UPDATE trades SET status='CLOSED', exit=?, result=?, pnl_r=?, pnl_percent=?,
           closed_at=?, closed_ts=? WHERE trade_id=? AND status!='CLOSED'""",
        (exit_p, result, pnl_r, pct, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), time.time(), trade["trade_id"]),
    )
    return res.rowcount > 0


def manage_active_positions(current_price: float) -> None:
    conn = db()
    try:
        active_trades = conn.execute("SELECT * FROM trades WHERE status IN ('OPEN','RISK_FREE')").fetchall()
        for t in active_trades:
            is_long = t["direction"] == "LONG"
            entry, sl, tp1, tp2, status = t["entry"], t["sl"], t["tp1"], t["tp2"], t["status"]
            tag = escape(t["signal_type"] or "")

            target_reached = (lambda lvl: current_price >= lvl) if is_long else (lambda lvl: current_price <= lvl)
            stopped_out = (lambda lvl: current_price <= lvl) if is_long else (lambda lvl: current_price >= lvl)

            # Target 2 Hit (+3.5R Final)
            if target_reached(tp2):
                if close_trade_record(conn, t, tp2, f"TP2 TARGET HIT (+{TP2_RR}R)", TP2_RR):
                    p_ret = pnl_percentage(t["direction"], entry, tp2)
                    send_telegram(
                        f"🔥 <b>[{tag} TP2 TARGET ACHIEVED]</b>\n"
                        f"Exit: <code>${tp2:,.2f}</code> | Return: <b>{p_ret:+.2f}%</b> (+{TP2_RR}R)\n"
                        f"Final target closed successfully!"
                    )
                continue

            # Target 1 Hit (+2.0R) -> Move SL to Breakeven
            if status == "OPEN" and target_reached(tp1):
                r = conn.execute("UPDATE trades SET status='RISK_FREE', sl=? WHERE trade_id=? AND status='OPEN'",
                                 (entry, t["trade_id"]))
                if r.rowcount > 0:
                    send_telegram(
                        f"🎯 <b>[{tag} TP1 HIT (+{TP1_RR}R)]</b>\n"
                        f"Stop Loss updated to Entry: <code>${entry:,.2f}</code>.\n"
                        f"Trade is now <b>100% Risk-Free</b>!"
                    )

            # SL Trigger
            if stopped_out(sl):
                is_full_loss = (status == "OPEN")
                res_label = "SL HIT (-1R)" if is_full_loss else "BREAKEVEN EXIT (0R)"
                pnl_r = -1.0 if is_full_loss else 0.0
                if close_trade_record(conn, t, sl, res_label, pnl_r):
                    p_ret = pnl_percentage(t["direction"], entry, sl)
                    badge = "❌" if is_full_loss else "⚖️"
                    send_telegram(
                        f"{badge} <b>[{tag} CLOSED]</b> {res_label}\n"
                        f"Exit: <code>${sl:,.2f}</code> | PnL: <b>{p_ret:+.2f}%</b>"
                    )
                    if is_full_loss and is_account_locked(conn):
                        send_telegram("⛔ <b>Institutional Drawdown Lock:</b> 2 consecutive losses. Scanner frozen for 24h.")
    finally:
        conn.close()


def try_dispatch_new_signal(current_price: float) -> None:
    with STATE_LOCK:
        klines = list(DATA_HUB["klines"])
        pdh, pdl, session = DATA_HUB["pdh"], DATA_HUB["pdl"], DATA_HUB["session"]
        fresh_data = (
            (time.time() - DATA_HUB["price_ts"] < DATA_MAX_AGE_SEC)
            and (time.time() - DATA_HUB["kline_ts"] < 120)
        )

    if not fresh_data or session not in ALLOWED_SESSIONS:
        return

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = db()
    try:
        if is_account_locked(conn):
            return

        active_count = conn.execute("SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','RISK_FREE')").fetchone()[0]
        today_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE created_date=?", (today_str,)).fetchone()[0]

        if active_count > 0 or today_trades >= MAX_DAILY_TRADES:
            return

        sig = scan_top_rated_engine(klines, pdh, pdl, current_price, session)
        if sig:
            t_id = f"PRO_{sig['time']}_{sig['cat']}"
            t_now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            try:
                conn.execute(
                    """INSERT INTO trades 
                    (trade_id, created_date, created_at, signal_type, tf, direction, setup, entry, sl, tp1, tp2, status, risk, session) 
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t_id, today_str, t_now, sig["cat"], "15M", sig["dir"], sig["setup"],
                     sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], "OPEN", sig["risk"], session),
                )
                dir_badge = "🟢 LONG" if sig["dir"] == "LONG" else "🔴 SHORT"
                send_telegram(
                    f"👑 <b>[INSTITUTIONAL GRADE ENTRY CONFIRMED]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Asset:</b> BTC/USD (Spot & Perp)\n"
                    f"<b>Direction:</b> {dir_badge} ({sig['cat']})\n"
                    f"<b>Session:</b> {session}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔹 <b>Entry:</b> <code>${sig['entry']:,.2f}</code>\n"
                    f"🛑 <b>Stop Loss:</b> <code>${sig['sl']:,.2f}</code> (Risk: ${sig['risk']:.1f})\n"
                    f"🎯 <b>Target 1 (1:{TP1_RR}):</b> <code>${sig['tp1']:,.2f}</code>\n"
                    f"🔥 <b>Target 2 (1:{TP2_RR}):</b> <code>${sig['tp2']:,.2f}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🧠 <b>Confluence:</b> {sig['setup']}\n"
                    f"🛡️ <b>Risk Engine:</b> Auto-Breakeven at TP1"
                )
            except sqlite3.IntegrityError:
                pass
    finally:
        conn.close()


# ==========================================
# MASTER LOOP
# ==========================================
def master_scanner_cycle() -> None:
    last_daily_sync = 0.0
    last_kline_sync = 0.0

    while True:
        try:
            now = time.time()
            if now - last_daily_sync > 300.0:
                fetch_daily_pdh_pdl()
                last_daily_sync = now

            if now - last_kline_sync > 15.0:
                fetch_15m_klines()
                last_kline_sync = now

            fetch_spot_price()
            price = DATA_HUB["price"]

            if price > 10000.0 and acquire_leader():
                manage_active_positions(price)
                try_dispatch_new_signal(price)

        except Exception as exc:
            logging.error(f"Cycle execution error: {exc}")

        time.sleep(3.0)


# Start Background Threads
threading.Thread(target=telegram_worker_loop, name="TelegramDispatcher", daemon=True).start()
threading.Thread(target=master_scanner_cycle, name="MasterScanner", daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
