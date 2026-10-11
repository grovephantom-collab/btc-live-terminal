#!/usr/bin/env python3
"""BTCUSDT SMC PAPER engine. Live/testnet order placement is intentionally disabled."""
import os, sys, time, uuid, hmac, math, json, queue, sqlite3, logging, threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)sZ [%(levelname)s] %(message)s"
)
log = logging.getLogger("SMC")


def required(name: str, n: int = 24) -> str:
    v = os.getenv(name, "").strip()
    if len(v) < n:
        raise RuntimeError(f"{name} is required and must be at least {n} characters")
    return v


def fenv(name, default, minimum=0.0):
    v = float(os.getenv(name, str(default)))
    if not math.isfinite(v) or v < minimum:
        raise RuntimeError(f"{name} must be finite and >= {minimum}")
    return v


def ienv(name, default, minimum=0):
    v = int(os.getenv(name, str(default)))
    if v < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return v


try:
    API_SECRET_KEY = required("API_SECRET_KEY")
    WORKER_AUTH_TOKEN = required("WORKER_AUTH_TOKEN")

    MODE = os.getenv("TRADING_MODE", "PAPER").strip().upper()
    if MODE != "PAPER":
        raise RuntimeError(
            "This worker.py is PAPER-only; TESTNET/LIVE order execution is disabled."
        )

    SYMBOL = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
    if not SYMBOL.isalnum():
        raise RuntimeError("Invalid SYMBOL")

    PORT = ienv("PORT", 10000, 1)
    if PORT > 65535:
        raise RuntimeError("PORT must be <= 65535")

    DB_FILE = os.getenv("DB_FILE", "data/institutional_vault.db").strip()
    BASE_URL = os.getenv(
        "BINANCE_FUTURES_URL", "https://fapi.binance.com"
    ).rstrip("/")

    HTTP_TIMEOUT = fenv("REQUEST_TIMEOUT_SEC", 7, 1)
    SCAN_SECONDS = fenv("SCAN_INTERVAL_SEC", 15, 3)
    LEASE_TTL = fenv("LEADER_TTL_SEC", 15, 5)
    MAX_DATA_AGE = fenv("MAX_DATA_AGE_SEC", 90, 10)

    START_EQUITY = fenv("PAPER_EQUITY_USD", 1000, 10)
    RISK_PCT = fenv("RISK_PER_TRADE_PCT", 0.01, 0.0001)
    MAX_DAILY_LOSS = fenv("MAX_DAILY_LOSS_USD", 50, 1)
    MAX_DAILY_TRADES = ienv("MAX_DAILY_TRADES", 3, 1)
    MAX_OPEN_TRADES = ienv("MAX_OPEN_TRADES", 1, 1)
    MAX_NOTIONAL = fenv("MAX_NOTIONAL_USD", 1000, 10)

    FEE_RATE = fenv("PAPER_FEE_RATE", 0.0005, 0)
    SLIPPAGE = fenv("PAPER_SLIPPAGE_RATE", 0.0003, 0)
    MIN_SCORE = fenv("MIN_SIGNAL_SCORE", 75, 0)

    MIN_GAP = fenv("MIN_FVG_GAP_USD", 35, 0)
    SWEEP_BUFFER = fenv("SWEEP_BUFFER_USD", 10, 0)
    SL_BUFFER = fenv("STRUCTURAL_SL_BUFFER_USD", 25, 0)
    MIN_STOP = fenv("MIN_STOP_DISTANCE_USD", 40, 1)
    MAX_STOP = fenv("MAX_STOP_DISTANCE_USD", 1500, 1)

    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    ALLOWED_SESSIONS = {
        x.strip().upper()
        for x in os.getenv(
            "ALLOWED_SESSIONS",
            "LONDON EXPANSION,NEW YORK TREND"
        ).split(",")
        if x.strip()
    }

except Exception as e:
    log.critical("Configuration error: %s", e)
    sys.exit(1)


INSTANCE = str(uuid.uuid4())
STARTED = time.time()
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Institutional-SMC-Paper/1.0"})

TG_QUEUE: queue.Queue = queue.Queue(maxsize=100)
LOCK = threading.Lock()

MARKET: Dict[str, Any] = {
    "price": None,
    "price_ts": 0,
    "klines": {},
    "klines_ts": 0,
    "session": "INITIALIZING",
    "poc": None,
    "error": None
}

STATE: Dict[str, Any] = {
    "status": "STARTING",
    "last_scan_epoch": 0,
    "last_signal": None,
    "last_error": None,
    "fencing_token": None
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    return datetime.now(timezone.utc).date().isoformat()


def jd(x):
    return json.dumps(x, separators=(",", ":"), sort_keys=True, default=str)


def session_now():
    h = datetime.now(timezone.utc).hour
    if h < 7:
        return "ASIAN RANGE"
    if h < 13:
        return "LONDON EXPANSION"
    if h < 21:
        return "NEW YORK TREND"
    return "SESSION CLOSE"


def db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
    c = sqlite3.connect(DB_FILE, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=20000")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    c = db()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leader_lease(
            id INTEGER PRIMARY KEY CHECK(id=1),
            leader_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            acquired_at_utc TEXT NOT NULL,
            expires_at_epoch REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT UNIQUE NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_name TEXT NOT NULL,
            score REAL NOT NULL,
            entry_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            tp1_price REAL NOT NULL,
            tp2_price REAL NOT NULL,
            tp3_price REAL NOT NULL,
            timeframe TEXT NOT NULL,
            candle_time INTEGER NOT NULL,
            details_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT UNIQUE NOT NULL,
            signal_key TEXT UNIQUE NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_name TEXT NOT NULL,
            score REAL NOT NULL,
            entry_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            tp1_price REAL NOT NULL,
            tp2_price REAL NOT NULL,
            tp3_price REAL NOT NULL,
            initial_qty REAL NOT NULL,
            remaining_qty REAL NOT NULL,
            tp1_done INTEGER NOT NULL DEFAULT 0,
            tp2_done INTEGER NOT NULL DEFAULT 0,
            tp3_done INTEGER NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT 'OPEN',
            realized_pnl_usd REAL NOT NULL DEFAULT 0,
            unrealized_pnl_usd REAL NOT NULL DEFAULT 0,
            total_fee_usd REAL NOT NULL DEFAULT 0,
            weighted_pnl_r REAL NOT NULL DEFAULT 0,
            close_reason TEXT,
            opened_at_utc TEXT NOT NULL,
            closed_at_utc TEXT,
            updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trade_audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL,
            FOREIGN KEY(trade_id)
                REFERENCES trades(trade_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_trades_stage ON trades(stage);
        CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at_utc);
        CREATE INDEX IF NOT EXISTS idx_audit_trade ON trade_audit_log(trade_id);
        """)
    finally:
        c.close()


def audit(c, tid, event, details):
    c.execute(
        """INSERT INTO trade_audit_log
        (trade_id,event_type,details_json,timestamp_utc)
        VALUES(?,?,?,?)""",
        (tid, event, jd(details), now_iso())
    )


def lease_tick() -> Tuple[bool, Optional[int]]:
    t = time.time()
    c = db()

    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute(
            "SELECT * FROM leader_lease WHERE id=1"
        ).fetchone()

        if r is None:
            token = 1
            c.execute(
                """INSERT INTO leader_lease
                VALUES(1,?,?,?,?)""",
                (INSTANCE, token, now_iso(), t + LEASE_TTL)
            )
            c.commit()
            return True, token

        if r["leader_id"] == INSTANCE and r["expires_at_epoch"] > t:
            token = int(r["fencing_token"])
            n = c.execute(
                """UPDATE leader_lease
                SET acquired_at_utc=?,expires_at_epoch=?
                WHERE id=1 AND leader_id=? AND fencing_token=?
                AND expires_at_epoch>?""",
                (now_iso(), t + LEASE_TTL, INSTANCE, token, t)
            )
            c.commit()
            return n == 1, token if n == 1 else None

        if r["expires_at_epoch"] <= t:
            token = int(r["fencing_token"]) + 1
            n = c.execute(
                """UPDATE leader_lease
                SET leader_id=?,fencing_token=?,acquired_at_utc=?,
                    expires_at_epoch=?
                WHERE id=1 AND fencing_token=? AND expires_at_epoch<=?""",
                (
                    INSTANCE, token, now_iso(), t + LEASE_TTL,
                    int(r["fencing_token"]), t
                )
            )
            c.commit()
            return n == 1, token if n == 1 else None

        c.commit()
        return False, None

    except Exception:
        c.rollback()
        log.exception("Leader lease failure")
        return False, None

    finally:
        c.close()


def leader_ok(token):
    if token is None:
        return False

    c = db()
    try:
        r = c.execute(
            "SELECT * FROM leader_lease WHERE id=1"
        ).fetchone()

        return bool(
            r
            and r["leader_id"] == INSTANCE
            and int(r["fencing_token"]) == int(token)
            and r["expires_at_epoch"] > time.time()
        )
    finally:
        c.close()


def tg(message):
    if TG_TOKEN and TG_CHAT:
        try:
            TG_QUEUE.put_nowait(message[:3500])
        except queue.Full:
            log.warning("Telegram queue full; alert dropped")


def tg_loop():
    while True:
        msg = TG_QUEUE.get()
        try:
            if TG_TOKEN and TG_CHAT:
                for attempt in range(4):
                    try:
                        r = HTTP.post(
                            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                            json={
                                "chat_id": TG_CHAT,
                                "text": msg,
                                "disable_web_page_preview": True
                            },
                            timeout=8
                        )

                        if r.status_code == 200:
                            break

                        if r.status_code == 429:
                            wait = float(
                                r.json().get("parameters", {}).get(
                                    "retry_after", 3
                                )
                            )
                            time.sleep(min(30, max(1, wait)))

                        elif r.status_code >= 500:
                            time.sleep(1.5 * (attempt + 1))
                        else:
                            log.warning("Telegram HTTP %s", r.status_code)
                            break

                    except requests.RequestException:
                        time.sleep(1.5 * (attempt + 1))
        finally:
            TG_QUEUE.task_done()


def fetch_price():
    r = HTTP.get(
        BASE_URL + "/fapi/v1/ticker/price",
        params={"symbol": SYMBOL},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    p = float(r.json()["price"])
    return p if math.isfinite(p) and p > 0 else None


def fetch_klines(interval, limit=200):
    r = HTTP.get(
        BASE_URL + "/fapi/v1/klines",
        params={
            "symbol": SYMBOL,
            "interval": interval,
            "limit": min(limit + 2, 1500)
        },
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()

    raw = r.json()
    now_ms = int(time.time() * 1000)
    out = []

    if not isinstance(raw, list):
        raise ValueError("Invalid kline response")

    for k in raw:
        if not isinstance(k, list) or len(k) < 7:
            continue

        if int(k[6]) >= now_ms:
            continue

        x = {
            "time": int(k[0] // 1000),
            "close_time": int(k[6] // 1000),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": float(k[5])
        }

        if (
            min(x["open"], x["low"], x["close"]) <= 0
            or x["high"] < max(x["open"], x["close"], x["low"])
            or x["vol"] < 0
        ):
            continue

        out.append(x)

    out.sort(key=lambda z: z["time"])
    dedup = {x["time"]: x for x in out}
    return list(dedup.values())[-limit:]


def approx_poc(cs, bins=30):
    if not cs:
        return None

    lo = min(c["low"] for c in cs)
    hi = max(c["high"] for c in cs)

    if hi <= lo:
        return lo

    width = (hi - lo) / bins
    vol = [0.0] * bins

    for c in cs:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        i = min(bins - 1, max(0, int((typical - lo) / width)))
        vol[i] += c["vol"]

    i = max(range(bins), key=lambda n: vol[n])
    return lo + (i + 0.5) * width


def refresh():
    try:
        p = fetch_price()
        ks = {
            k: fetch_klines(k, 200)
            for k in ("15m", "1h", "4h", "1d")
        }

        if (
            p is None
            or len(ks["15m"]) < 40
            or len(ks["1h"]) < 50
            or len(ks["4h"]) < 55
            or len(ks["1d"]) < 3
        ):
            raise ValueError("Insufficient valid market data/history")

        poc_val = approx_poc(ks["15m"][-96:])

        with LOCK:
            MARKET.update(
                price=p,
                price_ts=time.time(),
                klines=ks,
                klines_ts=time.time(),
                session=session_now(),
                poc=poc_val,
                error=None
            )

        return True

    except Exception as e:
        with LOCK:
            MARKET["error"] = str(e)[:250]
        log.warning("Market refresh failed: %s", e)
        return False


def ema(values: List[float], period: int) -> Optional[float]:
    if (
        period <= 0
        or len(values) < period
        or any(not math.isfinite(v) or v <= 0 for v in values)
    ):
        return None

    k = 2 / (period + 1)
    v = sum(values[:period]) / period

    for x in values[period:]:
        v += k * (x - v)

    return v


def atr(cs, period=14):
    if len(cs) < period + 1:
        return None

    tr = []
    for i in range(1, len(cs)):
        x, pr = cs[i], cs[i - 1]
        tr.append(max(
            x["high"] - x["low"],
            abs(x["high"] - pr["close"]),
            abs(x["low"] - pr["close"])
        ))

    return sum(tr[-period:]) / period


def psyche(c):
    rng = max(c["high"] - c["low"], 1e-9)
    body = abs(c["close"] - c["open"])

    return {
        "body": body / rng,
        "upper": (c["high"] - max(c["open"], c["close"])) / rng,
        "lower": (min(c["open"], c["close"]) - c["low"]) / rng
    }


def swings(cs, n=3):
    hs, ls = [], []

    for i in range(n, len(cs) - n):
        c = cs[i]

        if (
            all(c["high"] > cs[i-j]["high"] for j in range(1, n+1))
            and all(c["high"] >= cs[i+j]["high"] for j in range(1, n+1))
        ):
            hs.append({"price": c["high"], "time": c["time"]})

        if (
            all(c["low"] < cs[i-j]["low"] for j in range(1, n+1))
            and all(c["low"] <= cs[i+j]["low"] for j in range(1, n+1))
        ):
            ls.append({"price": c["low"], "time": c["time"]})

    return hs, ls


def fvgs(cs):
    out = []

    for i in range(1, len(cs) - 1):
        a, m, b = cs[i-1], cs[i], cs[i+1]
        rng = max(m["high"] - m["low"], 1e-9)

        if abs(m["close"] - m["open"]) / rng < 0.50:
            continue

        if b["low"] > a["high"] and b["low"] - a["high"] >= MIN_GAP:
            out.append({
                "direction": "LONG",
                "top": b["low"],
                "bottom": a["high"],
                "ce": (b["low"] + a["high"]) / 2,
                "time": m["time"]
            })

        elif a["low"] > b["high"] and a["low"] - b["high"] >= MIN_GAP:
            out.append({
                "direction": "SHORT",
                "top": a["low"],
                "bottom": b["high"],
                "ce": (a["low"] + b["high"]) / 2,
                "time": m["time"]
            })

    return out


def structure(ks):
    c15, c1h, c4h, c1d = (
        ks["15m"], ks["1h"], ks["4h"], ks["1d"]
    )

    e20 = ema([c["close"] for c in c4h], 20)
    e50 = ema([c["close"] for c in c4h], 50)
    h20 = ema([c["close"] for c in c1h], 20)
    h50 = ema([c["close"] for c in c1h], 50)

    bias = (
        "UNKNOWN" if e20 is None or e50 is None
        else "BULLISH" if e20 > e50
        else "BEARISH" if e20 < e50
        else "NEUTRAL"
    )

    b1 = (
        "UNKNOWN" if h20 is None or h50 is None
        else "BULLISH" if h20 > h50
        else "BEARISH" if h20 < h50
        else "NEUTRAL"
    )

    sh, sl = swings(c15)
    recent = c15[-30:]

    swing_hi = sh[-1]["price"] if sh else max(c["high"] for c in recent)
    swing_lo = sl[-1]["price"] if sl else min(c["low"] for c in recent)

    return {
        "bias4h": bias,
        "bias1h": b1,
        "swing_high": swing_hi,
        "swing_low": swing_lo,
        "pdh": c1d[-1]["high"],
        "pdl": c1d[-1]["low"],
        "eq": (swing_hi + swing_lo) / 2,
        "atr": atr(c15),
        "poc": approx_poc(c15[-96:]),
        "fvgs": fvgs(c15)
    }


def make_signal(st, cs, price, sess):
    if (
        len(cs) < 40
        or st["bias4h"] == "UNKNOWN"
        or st["bias1h"] == "UNKNOWN"
        or not st["atr"]
        or sess not in ALLOWED_SESSIONS
    ):
        return None

    swept, trigger = cs[-2], cs[-1]
    p = psyche(swept)
    eq = st["eq"]
    candidates = []

    # 1. SETUP: LIQUIDITY SWEEP & RECLAIM (LONG)
    for label, level in [
        ("PDL", st["pdl"]),
        ("SWING_LOW", st["swing_low"])
    ]:
        valid = (
            swept["low"] < level - SWEEP_BUFFER
            and trigger["close"] > level
            and trigger["close"] > trigger["open"]
            and price < eq
            and p["lower"] >= 0.35
            and p["body"] <= 0.65
            and st["bias4h"] != "BEARISH"
        )

        if valid:
            sl = min(swept["low"], trigger["low"]) - max(
                SL_BUFFER, 0.15 * st["atr"]
            )
            risk = price - sl

            if MIN_STOP <= risk <= MAX_STOP:
                score = min(
                    100,
                    70 + min(12, p["lower"] * 15)
                    + (8 if label == "PDL" else 4)
                    + (3 if st["poc"] and price < st["poc"] else 0)
                )

                candidates.append({
                    "direction": "LONG",
                    "setup_name": f"{label} SSL sweep/reclaim",
                    "entry": price,
                    "sl": sl,
                    "risk": risk,
                    "score": score,
                    "swept_level": label,
                    "trigger_time": trigger["time"],
                    "wick_ratio": p["lower"]
                })

    # 2. SETUP: LIQUIDITY SWEEP & RECLAIM (SHORT)
    for label, level in [
        ("PDH", st["pdh"]),
        ("SWING_HIGH", st["swing_high"])
    ]:
        valid = (
            swept["high"] > level + SWEEP_BUFFER
            and trigger["close"] < level
            and trigger["close"] < trigger["open"]
            and price > eq
            and p["upper"] >= 0.35
            and p["body"] <= 0.65
            and st["bias4h"] != "BULLISH"
        )

        if valid:
            sl = max(swept["high"], trigger["high"]) + max(
                SL_BUFFER, 0.15 * st["atr"]
            )
            risk = sl - price

            if MIN_STOP <= risk <= MAX_STOP:
                score = min(
                    100,
                    70 + min(12, p["upper"] * 15)
                    + (8 if label == "PDH" else 4)
                    + (3 if st["poc"] and price > st["poc"] else 0)
                )

                candidates.append({
                    "direction": "SHORT",
                    "setup_name": f"{label} BSL sweep/reclaim",
                    "entry": price,
                    "sl": sl,
                    "risk": risk,
                    "score": score,
                    "swept_level": label,
                    "trigger_time": trigger["time"],
                    "wick_ratio": p["upper"]
                })

    # 3. SETUP: FVG CONSEQUENT ENCROACHMENT (CE) RETEST
    for f in st.get("fvgs", []):
        if f["direction"] == "LONG" and price < eq and st["bias4h"] == "BULLISH":
            if trigger["low"] <= f["ce"] and trigger["close"] > f["ce"] and trigger["close"] > trigger["open"]:
                sl = f["bottom"] - max(SL_BUFFER, 0.15 * st["atr"])
                risk = price - sl
                if MIN_STOP <= risk <= MAX_STOP:
                    candidates.append({
                        "direction": "LONG",
                        "setup_name": "FVG CE Retest in Discount",
                        "entry": price,
                        "sl": sl,
                        "risk": risk,
                        "score": 80.0,
                        "swept_level": "FVG_CE",
                        "trigger_time": trigger["time"],
                        "wick_ratio": p["lower"]
                    })
        elif f["direction"] == "SHORT" and price > eq and st["bias4h"] == "BEARISH":
            if trigger["high"] >= f["ce"] and trigger["close"] < f["ce"] and trigger["close"] < trigger["open"]:
                sl = f["top"] + max(SL_BUFFER, 0.15 * st["atr"])
                risk = sl - price
                if MIN_STOP <= risk <= MAX_STOP:
                    candidates.append({
                        "direction": "SHORT",
                        "setup_name": "FVG CE Retest in Premium",
                        "entry": price,
                        "sl": sl,
                        "risk": risk,
                        "score": 80.0,
                        "swept_level": "FVG_CE",
                        "trigger_time": trigger["time"],
                        "wick_ratio": p["upper"]
                    })

    if not candidates:
        return None

    s = max(candidates, key=lambda x: x["score"])
    if s["score"] < MIN_SCORE:
        return None

    sign = 1 if s["direction"] == "LONG" else -1

    s.update(
        tp1=s["entry"] + sign * 1.5 * s["risk"],
        tp2=s["entry"] + sign * 2.25 * s["risk"],
        tp3=s["entry"] + sign * 3.0 * s["risk"],
        timeframe="15m",
        session=sess,
        structure={
            "bias4h": st["bias4h"],
            "bias1h": st["bias1h"],
            "eq": eq,
            "pdh": st["pdh"],
            "pdl": st["pdl"],
            "swing_high": st["swing_high"],
            "swing_low": st["swing_low"],
            "poc_approx": st["poc"]
        }
    )

    return s


def equity(c):
    r = c.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades"
    ).fetchone()
    return max(0, START_EQUITY + float(r["p"] or 0.0))


def daily_stats(c):
    day = today()

    rows = c.execute(
        """SELECT realized_pnl_usd FROM trades
        WHERE substr(opened_at_utc,1,10)=?""",
        (day,)
    ).fetchall()

    count = c.execute(
        """SELECT COUNT(*) n FROM trades
        WHERE substr(opened_at_utc,1,10)=?""",
        (day,)
    ).fetchone()["n"]

    return sum(float(r["realized_pnl_usd"] or 0.0) for r in rows), int(count or 0)


def open_paper_trade(s, token):
    if not leader_ok(token):
        return False

    key = (
        f"{SYMBOL}:{s['direction']}:"
        f"{s['trigger_time']}:{s['swept_level']}"
    )

    c = db()

    try:
        c.execute("BEGIN IMMEDIATE")

        lease = c.execute(
            "SELECT * FROM leader_lease WHERE id=1"
        ).fetchone()

        if (
            not lease
            or lease["leader_id"] != INSTANCE
            or int(lease["fencing_token"]) != token
            or lease["expires_at_epoch"] <= time.time()
        ):
            c.rollback()
            return False

        if c.execute(
            "SELECT 1 FROM signals WHERE signal_key=?", (key,)
        ).fetchone():
            c.rollback()
            return False

        pnl, n = daily_stats(c)
        op = c.execute(
            "SELECT COUNT(*) n FROM trades WHERE stage!='CLOSED'"
        ).fetchone()["n"]

        if pnl <= -MAX_DAILY_LOSS or n >= MAX_DAILY_TRADES or op >= MAX_OPEN_TRADES:
            c.rollback()
            return False

        qty = min(
            (equity(c) * RISK_PCT) / s["risk"],
            MAX_NOTIONAL / s["entry"]
        )

        if not math.isfinite(qty) or qty <= 0:
            c.rollback()
            return False

        c.execute(
            """INSERT INTO signals(
                signal_key,symbol,direction,setup_name,score,
                entry_price,stop_price,tp1_price,tp2_price,tp3_price,
                timeframe,candle_time,details_json,created_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, SYMBOL, s["direction"], s["setup_name"], s["score"],
                s["entry"], s["sl"], s["tp1"], s["tp2"], s["tp3"],
                s["timeframe"], s["trigger_time"], jd(s), now_iso()
            )
        )

        tid = str(uuid.uuid4())
        now = now_iso()

        c.execute(
            """INSERT INTO trades(
                trade_id,signal_key,symbol,direction,setup_name,score,
                entry_price,stop_price,tp1_price,tp2_price,tp3_price,
                initial_qty,remaining_qty,stage,opened_at_utc,updated_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)""",
            (
                tid, key, SYMBOL, s["direction"], s["setup_name"],
                s["score"], s["entry"], s["sl"], s["tp1"], s["tp2"],
                s["tp3"], qty, qty, now, now
            )
        )

        audit(c, tid, "PAPER_OPEN", {
            "qty": qty,
            "fencing_token": token,
            "signal_key": key
        })

        c.commit()

        log.info(
            "PAPER OPEN %s %s qty=%.6f entry=%.2f score=%.1f",
            s["direction"], tid[:8], qty, s["entry"], s["score"]
        )

        tg(
            f"PAPER {SYMBOL} {s['direction']}\n"
            f"{s['setup_name']}\nScore {s['score']:.1f}\n"
            f"Entry {s['entry']:.2f} | SL {s['sl']:.2f}\n"
            f"TPs {s['tp1']:.2f} / {s['tp2']:.2f} / {s['tp3']:.2f}\n"
            f"Qty {qty:.6f}"
        )
        return True

    except sqlite3.IntegrityError:
        c.rollback()
        return False

    except Exception:
        c.rollback()
        log.exception("Paper trade insert failed")
        return False

    finally:
        c.close()


def manage_trades(price, token):
    if not leader_ok(token) or price <= 0:
        return

    c = db()

    try:
        c.execute("BEGIN IMMEDIATE")
        l = c.execute(
            "SELECT * FROM leader_lease WHERE id=1"
        ).fetchone()

        if (
            not l
            or l["leader_id"] != INSTANCE
            or int(l["fencing_token"]) != token
            or l["expires_at_epoch"] <= time.time()
        ):
            c.rollback()
            return

        for r in c.execute(
            "SELECT * FROM trades WHERE stage!='CLOSED'"
        ).fetchall():

            direction = r["direction"]
            sign = 1 if direction == "LONG" else -1
            entry = float(r["entry_price"])
            qty = float(r["remaining_qty"])
            stop = float(r["stop_price"])
            realized = float(r["realized_pnl_usd"] or 0.0)
            fees = float(r["total_fee_usd"] or 0.0)
            stage = r["stage"]
            reason = None

            flags = {
                "tp1_done": r["tp1_done"],
                "tp2_done": r["tp2_done"],
                "tp3_done": r["tp3_done"]
            }

            def target_hit(level):
                return price >= level if direction == "LONG" else price <= level

            def stop_hit(level):
                return price <= level if direction == "LONG" else price >= level

            if stop_hit(stop):
                exitp = stop * (
                    1 - SLIPPAGE if sign == 1 else 1 + SLIPPAGE
                )
                gross = (exitp - entry) * qty * sign
                fee = abs(exitp * qty) * FEE_RATE

                realized += gross - fee
                fees += fee

                audit(c, r["trade_id"], "STOP_LOSS", {
                    "price": exitp,
                    "qty": qty,
                    "gross": gross,
                    "fee": fee
                })

                qty = 0
                reason = "STOP_LOSS"

            else:
                for n, field, fraction in [
                    (1, "tp1_price", 0.50),
                    (2, "tp2_price", 0.30),
                    (3, "tp3_price", 1.00)
                ]:
                    flag = f"tp{n}_done"

                    if flags[flag] or not target_hit(float(r[field])):
                        continue

                    close_qty = (
                        qty if n == 3
                        else min(qty, float(r["initial_qty"]) * fraction)
                    )

                    if close_qty <= 0:
                        continue

                    exitp = float(r[field]) * (
                        1 - SLIPPAGE if sign == 1 else 1 + SLIPPAGE
                    )

                    gross = (exitp - entry) * close_qty * sign
                    fee = abs(exitp * close_qty) * FEE_RATE

                    realized += gross - fee
                    fees += fee
                    qty = max(0, qty - close_qty)
                    flags[flag] = 1

                    audit(c, r["trade_id"], f"TP{n}_HIT", {
                        "price": exitp,
                        "qty": close_qty,
                        "gross": gross,
                        "fee": fee
                    })

                    if n == 1:
                        stop = entry
                        stage = "TP1_HIT"
                    elif n == 2:
                        stage = "TP2_HIT"
                    else:
                        reason = "TP3"
                        qty = 0

                    break

            if qty <= 1e-10:
                qty = 0
                stage = "CLOSED"
                reason = reason or "TARGETS_COMPLETE"

            unreal = (price - entry) * qty * sign
            risk = max(
                float(r["initial_qty"]) * abs(entry - float(r["stop_price"])),
                1e-9
            )
            wr = realized / risk

            c.execute(
                """UPDATE trades SET
                    stop_price=?,remaining_qty=?,
                    tp1_done=?,tp2_done=?,tp3_done=?,
                    stage=?,realized_pnl_usd=?,unrealized_pnl_usd=?,
                    total_fee_usd=?,weighted_pnl_r=?,close_reason=?,
                    closed_at_utc=?,updated_at_utc=?
                WHERE trade_id=?""",
                (
                    stop, qty,
                    flags["tp1_done"], flags["tp2_done"], flags["tp3_done"],
                    stage, realized, unreal, fees, wr, reason,
                    now_iso() if stage == "CLOSED" else None,
                    now_iso(), r["trade_id"]
                )
            )

            if reason:
                tg(
                    f"PAPER {SYMBOL} {direction} {reason}\n"
                    f"Realized PnL {realized:.2f} USD"
                )

        c.commit()

    except Exception:
        c.rollback()
        log.exception("Trade manager error")

    finally:
        c.close()


def engine_loop():
    while True:
        try:
            is_leader, token = lease_tick()

            if not is_leader or token is None:
                STATE.update(status="STANDBY", fencing_token=None)
                time.sleep(2)
                continue

            STATE.update(status="RUNNING_PAPER", fencing_token=token)

            if not refresh():
                STATE.update(last_error=MARKET.get("error"))
                time.sleep(SCAN_SECONDS)
                continue

            with LOCK:
                p = MARKET["price"]
                pts = MARKET["price_ts"]
                ks = {k: list(v) for k, v in MARKET["klines"].items()}
                sess = MARKET["session"]
                kts = MARKET["klines_ts"]

            if (
                time.time() - pts > MAX_DATA_AGE
                or time.time() - kts > MAX_DATA_AGE
            ):
                STATE["status"] = "DATA_STALE"
                time.sleep(SCAN_SECONDS)
                continue

            manage_trades(p, token)

            st = structure(ks)
            s = make_signal(st, ks["15m"], p, sess)

            if s:
                STATE["last_signal"] = {
                    "direction": s["direction"],
                    "score": s["score"],
                    "setup_name": s["setup_name"],
                    "time": now_iso()
                }
                open_paper_trade(s, token)

            STATE.update(
                last_scan_epoch=time.time(),
                last_error=None
            )

            time.sleep(SCAN_SECONDS)

        except Exception as e:
            STATE.update(status="ERROR", last_error=str(e)[:250])
            log.exception("Engine loop exception")
            time.sleep(max(3, SCAN_SECONDS))


app = Flask(__name__)


def auth_ok():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return False

    return hmac.compare_digest(
        h[7:].strip().encode(),
        WORKER_AUTH_TOKEN.encode()
    )


@app.before_request
def protect():
    if request.path == "/healthz":
        return None

    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401


@app.get("/healthz")
def healthz():
    with LOCK:
        fresh = bool(
            MARKET["price"]
            and time.time() - MARKET["price_ts"] <= MAX_DATA_AGE
            and time.time() - MARKET["klines_ts"] <= MAX_DATA_AGE
        )

    d = dict(STATE)
    d.update(
        mode="PAPER",
        symbol=SYMBOL,
        market_data_fresh=fresh,
        uptime_seconds=int(time.time() - STARTED),
        utc_time=now_iso()
    )

    code = 200 if d["status"] in ("RUNNING_PAPER", "STANDBY") else 503
    return jsonify(d), code


@app.get("/vault-data")
def vault_data():
    limit = min(max(int(request.args.get("limit", "50")), 1), 200)
    c = db()

    try:
        ts = [
            dict(r) for r in c.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        ]

        active_list = [
            dict(r) for r in c.execute(
                "SELECT * FROM trades WHERE stage!='CLOSED' ORDER BY id DESC LIMIT 1"
            ).fetchall()
        ]

        ss = [
            dict(r) for r in c.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        ]

        pnl = c.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades"
        ).fetchone()["p"]

        op = c.execute(
            "SELECT COUNT(*) n FROM trades WHERE stage!='CLOSED'"
        ).fetchone()["n"]

        wins = c.execute(
            "SELECT COUNT(*) n FROM trades WHERE stage='CLOSED' AND realized_pnl_usd > 0"
        ).fetchone()["n"]

        losses = c.execute(
            "SELECT COUNT(*) n FROM trades WHERE stage='CLOSED' AND realized_pnl_usd <= 0"
        ).fetchone()["n"]

        total_closed = wins + losses
        win_rate = round(100.0 * wins / total_closed, 1) if total_closed > 0 else 0.0

        with LOCK:
            curr_p = MARKET.get("price") or 0.0
            curr_sess = MARKET.get("session") or "UNKNOWN"
            curr_poc = MARKET.get("poc") or 0.0

        return jsonify({
            "mode": "PAPER",
            "symbol": SYMBOL,
            "starting_equity_usd": START_EQUITY,
            "equity_estimate_usd": equity(c),
            "realized_pnl_usd": float(pnl or 0.0),
            "open_trades": op,
            "price": curr_p,
            "session": curr_sess,
            "poc": curr_poc,
            "market": {
                "price": curr_p,
                "session": curr_sess,
                "poc": curr_poc
            },
            "stats": {
                "total_r": round(float(pnl or 0.0) / 10.0, 2),
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate
            },
            "active_trade": active_list[0] if active_list else None,
            "active": active_list,
            "vault": ts,
            "history": ts,
            "trades": ts,
            "signals": ss
        })

    finally:
        c.close()


@app.get("/signals")
def signals_api():
    limit = min(max(int(request.args.get("limit", "20")), 1), 100)
    c = db()

    try:
        rows = c.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        return jsonify({"signals": [dict(r) for r in rows]})
    finally:
        c.close()


@app.errorhandler(413)
def too_big(_):
    return jsonify({"error": "request too large"}), 413


def main():
    init_db()

    threading.Thread(
        target=tg_loop, daemon=True, name="telegram"
    ).start()

    threading.Thread(
        target=engine_loop, daemon=True, name="engine"
    ).start()

    log.info(
        "Starting %s in PAPER-only mode on port %d; exchange order placement disabled.",
        SYMBOL, PORT
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        threaded=True,
        use_reloader=False
    )


if __name__ == "__main__":
    main()
