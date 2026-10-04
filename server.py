import os
import json
import time
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go

# ============================================================
# BTCUSDT SMC RADAR — COMPLETE SINGLE FILE STREAMLIT ENGINE
# Public Binance Futures data + Telegram + SQLite Vault
# ============================================================

st.set_page_config(
    page_title="BTCUSDT SMC RADAR",
    layout="wide",
    initial_sidebar_state="collapsed",
)

SYMBOL = "BTCUSDT"
BASE = "https://fapi.binance.com"
DB_FILE = os.getenv("SMC_DB_FILE", "smc_vault.db")

# HARDCODED INTEGRATED TELEGRAM DETAILS
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

TF_LIMITS = {
    "5m": 800,
    "15m": 500,
    "1h": 500,
    "4h": 300,
}

LOCK = threading.RLock()


# ----------------------------- UTILITIES -----------------------------

def now_utc():
    return datetime.now(timezone.utc)


def fmt_price(x):
    return f"${x:,.2f}" if x is not None and not np.isnan(x) else "--"


def safe_float(x, default=np.nan):
    try:
        return float(x)
    except Exception:
        return default


def telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# ----------------------------- DATABASE -----------------------------

def db_init():
    with sqlite3.connect(DB_FILE, timeout=10) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            setup TEXT NOT NULL,
            direction TEXT NOT NULL,
            score INTEGER NOT NULL,
            entry REAL NOT NULL,
            sl REAL NOT NULL,
            tp1 REAL NOT NULL,
            tp2 REAL NOT NULL,
            tp3 REAL NOT NULL,
            exit REAL,
            result TEXT,
            pnl_r REAL,
            notes TEXT
        )
        """)
        con.commit()


def db_insert_trade(t):
    with sqlite3.connect(DB_FILE, timeout=10) as con:
        con.execute("""
        INSERT INTO trades
        (created_at,setup,direction,score,entry,sl,tp1,tp2,tp3,exit,result,pnl_r,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            t.created_at, t.setup, t.direction, t.score,
            t.entry, t.sl, t.tp1, t.tp2, t.tp3,
            None, "OPEN", None, t.notes
        ))
        con.commit()


def db_close_trade(exit_price, result, pnl_r):
    with sqlite3.connect(DB_FILE, timeout=10) as con:
        row = con.execute(
            "SELECT id FROM trades WHERE result='OPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            con.execute("""
                UPDATE trades SET exit=?, result=?, pnl_r=? WHERE id=?
            """, (exit_price, result, pnl_r, row[0]))
            con.commit()


def db_rows():
    with sqlite3.connect(DB_FILE, timeout=10) as con:
        return con.execute("""
            SELECT created_at,setup,direction,score,entry,sl,tp1,tp2,tp3,
                   exit,result,pnl_r,notes
            FROM trades ORDER BY id DESC LIMIT 50
        """).fetchall()


# ----------------------------- BINANCE DATA -----------------------------

class BinanceClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "BTCUSDT-SMC-RADAR/1.0"})

    def klines(self, interval, limit=500):
        url = f"{BASE}/fapi/v1/klines"
        r = self.session.get(
            url,
            params={"symbol": SYMBOL, "interval": interval, "limit": limit},
            timeout=8,
        )
        r.raise_for_status()
        raw = r.json()

        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore"
        ]
        df = pd.DataFrame(raw, columns=cols)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df[["open_time","close_time","open","high","low","close","volume"]].copy()

    def ticker(self):
        r = self.session.get(
            f"{BASE}/fapi/v1/ticker/price",
            params={"symbol": SYMBOL},
            timeout=5,
        )
        r.raise_for_status()
        return float(r.json()["price"])

    def funding(self):
        r = self.session.get(
            f"{BASE}/fapi/v1/premiumIndex",
            params={"symbol": SYMBOL},
            timeout=5,
        )
        r.raise_for_status()
        j = r.json()
        return safe_float(j.get("lastFundingRate"), 0.0)

    def open_interest(self):
        r = self.session.get(
            f"{BASE}/fapi/v1/openInterest",
            params={"symbol": SYMBOL},
            timeout=5,
        )
        r.raise_for_status()
        return safe_float(r.json().get("openInterest"), 0.0)


# ----------------------------- SMC INDICATORS -----------------------------

def atr(df, period=14):
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def ema(df, n):
    return df["close"].ewm(span=n, adjust=False).mean()


def rsi(df, n=14):
    d = df["close"].diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def adx(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    down = -l.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([
        h-l, (h-c.shift()).abs(), (l-c.shift()).abs()
    ], axis=1).max(axis=1)
    atrv = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atrv
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atrv
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def swings(df, left=3, right=3):
    highs, lows = [], []
    h = df["high"].values
    l = df["low"].values
    for i in range(left, len(df)-right):
        if h[i] == max(h[i-left:i+right+1]):
            highs.append((i, h[i]))
        if l[i] == min(l[i-left:i+right+1]):
            lows.append((i, l[i]))
    return highs, lows


def structure_state(df):
    if len(df) < 30:
        return {"trend": "UNKNOWN", "bos": None, "choch": None, "last_swing_high": None, "last_swing_low": None}

    highs, lows = swings(df.tail(180).reset_index(drop=True), 3, 3)
    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "RANGE", "bos": None, "choch": None, "last_swing_high": None, "last_swing_low": None}

    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    last = float(df["close"].iloc[-1])

    if h2 > h1 and l2 > l1:
        trend = "BULL"
    elif h2 < h1 and l2 < l1:
        trend = "BEAR"
    else:
        trend = "RANGE"

    bos = None
    if last > h2:
        bos = "BULL_BOS"
    elif last < l2:
        bos = "BEAR_BOS"

    choch = None
    if trend == "BEAR" and last > h2:
        choch = "BULL_CHOCH"
    elif trend == "BULL" and last < l2:
        choch = "BEAR_CHOCH"

    return {"trend": trend, "bos": bos, "choch": choch,
            "last_swing_high": h2, "last_swing_low": l2}


def liquidity_levels(df):
    out = {}
    for n, label in [(576, "48H"), (288, "24H"), (48, "4H"), (12, "1H"), (3, "15M")]:
        x = df.iloc[:-1].tail(n)
        if len(x):
            out[label+"_HIGH"] = float(x["high"].max())
            out[label+"_LOW"] = float(x["low"].min())
    
    # 5M Previous Closed Candle Liquidity
    if len(df) >= 2:
        prev = df.iloc[-2]
        out["5M_HIGH"] = float(prev["high"])
        out["5M_LOW"] = float(prev["low"])
        
    return out


def detect_fvg(df):
    if len(df) < 3:
        return None
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]

    if c["low"] > a["high"]:
        return {
            "type": "BULL_FVG",
            "low": float(a["high"]),
            "high": float(c["low"]),
            "size": float(c["low"] - a["high"])
        }

    if c["high"] < a["low"]:
        return {
            "type": "BEAR_FVG",
            "low": float(c["high"]),
            "high": float(a["low"]),
            "size": float(a["low"] - c["high"])
        }

    return None


def detect_order_block(df):
    if len(df) < 8:
        return None

    a = df.iloc[-6:-1]
    current = df.iloc[-1]
    av_atr = float(atr(df, 14).iloc[-1])

    if not np.isfinite(av_atr) or av_atr <= 0:
        return None

    body = abs(float(current["close"] - current["open"]))

    if body >= 1.3 * av_atr:
        if current["close"] > current["open"]:
            for i in range(len(a)-1, -1, -1):
                x = a.iloc[i]
                if x["close"] < x["open"]:
                    return {
                        "type": "BULL_OB",
                        "low": float(x["low"]),
                        "high": float(x["high"])
                    }

        if current["close"] < current["open"]:
            for i in range(len(a)-1, -1, -1):
                x = a.iloc[i]
                if x["close"] > x["open"]:
                    return {
                        "type": "BEAR_OB",
                        "low": float(x["low"]),
                        "high": float(x["high"])
                    }

    return None


def sweep_signal(df):
    if len(df) < 600:
        return None

    current = df.iloc[-1]
    levels = liquidity_levels(df)

    checks = [
        ("48H", levels.get("48H_LOW"), levels.get("48H_HIGH")),
        ("24H", levels.get("24H_LOW"), levels.get("24H_HIGH")),
        ("4H", levels.get("4H_LOW"), levels.get("4H_HIGH")),
        ("1H", levels.get("1H_LOW"), levels.get("1H_HIGH")),
        ("15M", levels.get("15M_LOW"), levels.get("15M_HIGH")),
        ("5M", levels.get("5M_LOW"), levels.get("5M_HIGH")),
    ]

    for name, low, high in checks:
        if low is not None and current["low"] < low and current["close"] > low:
            return {"direction": "LONG", "pool": name,
                    "level": float(low), "extreme": float(current["low"])}

        if high is not None and current["high"] > high and current["close"] < high:
            return {"direction": "SHORT", "pool": name,
                    "level": float(high), "extreme": float(current["high"])}

    return None


# ----------------------------- SIGNAL ENGINE -----------------------------

@dataclass
class Signal:
    created_at: str
    direction: str
    setup: str
    score: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    atr: float
    notes: str


class SMCSignalEngine:
    def __init__(self):
        self.last_signal_key = None
        self.last_signal: Optional[Signal] = None
        self.active: Optional[Signal] = None
        self.last_error = ""
        self.last_scan = None
        self.data: Dict[str, pd.DataFrame] = {}
        self.market = {}

    def fetch(self):
        api = BinanceClient()
        for tf, limit in TF_LIMITS.items():
            self.data[tf] = api.klines(tf, limit)
        self.market["price"] = api.ticker()
        self.market["funding"] = api.funding()
        self.market["oi"] = api.open_interest()
        self.last_scan = now_utc().isoformat()

    def build_signal(self):
        df5 = self.data["5m"]
        df15 = self.data["15m"]
        df1 = self.data["1h"]
        df4 = self.data["4h"]

        closed5 = df5.iloc[:-1].copy()
        if len(closed5) < 600:
            return None

        price = float(self.market["price"])
        atr5 = float(atr(closed5, 14).iloc[-1])
        if not np.isfinite(atr5) or atr5 <= 0:
            return None

        st4 = structure_state(df4.iloc[:-1])
        st1 = structure_state(df1.iloc[:-1])
        st15 = structure_state(df15.iloc[:-1])
        st5 = structure_state(closed5)

        sweep = sweep_signal(closed5)
        fvg = detect_fvg(closed5)
        ob = detect_order_block(closed5)

        score_long = 0
        score_short = 0
        reasons_long, reasons_short = [], []

        if st4["trend"] == "BULL":
            score_long += 12; reasons_long.append("4H bullish")
        elif st4["trend"] == "BEAR":
            score_short += 12; reasons_short.append("4H bearish")

        if st1["trend"] == "BULL":
            score_long += 12; reasons_long.append("1H bullish")
        elif st1["trend"] == "BEAR":
            score_short += 12; reasons_short.append("1H bearish")

        if st15["trend"] == "BULL":
            score_long += 7; reasons_long.append("15M bullish")
        elif st15["trend"] == "BEAR":
            score_short += 7; reasons_short.append("15M bearish")

        if sweep:
            pts = {"48H": 24, "24H": 19, "4H": 15, "1H": 11, "15M": 7, "5M": 5}.get(sweep["pool"], 5)
            if sweep["direction"] == "LONG":
                score_long += pts
                reasons_long.append(f"{sweep['pool']} sell-side sweep")
            else:
                score_short += pts
                reasons_short.append(f"{sweep['pool']} buy-side sweep")

        if st5["bos"] == "BULL_BOS" or st5["choch"] == "BULL_CHOCH":
            score_long += 12; reasons_long.append("5M bullish structure shift")
        if st5["bos"] == "BEAR_BOS" or st5["choch"] == "BEAR_CHOCH":
            score_short += 12; reasons_short.append("5M bearish structure shift")

        if fvg and fvg["type"] == "BULL_FVG":
            score_long += 8; reasons_long.append("bullish FVG")
        elif fvg and fvg["type"] == "BEAR_FVG":
            score_short += 8; reasons_short.append("bearish FVG")

        if ob and ob["type"] == "BULL_OB":
            score_long += 8; reasons_long.append("bullish OB")
        elif ob and ob["type"] == "BEAR_OB":
            score_short += 8; reasons_short.append("bearish OB")

        r = float(rsi(closed5).iloc[-1])
        if np.isfinite(r):
            if 52 <= r <= 72:
                score_long += 5; reasons_long.append(f"RSI {r:.1f}")
            if 28 <= r <= 48:
                score_short += 5; reasons_short.append(f"RSI {r:.1f}")

        a = float(adx(closed5).iloc[-1])
        if np.isfinite(a) and a >= 20:
            if score_long > score_short:
                score_long += 4; reasons_long.append(f"ADX {a:.1f}")
            elif score_short > score_long:
                score_short += 4; reasons_short.append(f"ADX {a:.1f}")

        vol = closed5["volume"]
        if len(vol) >= 21:
            vr = float(vol.iloc[-1] / vol.iloc[-21:-1].mean())
            if vr >= 1.5:
                if closed5["close"].iloc[-1] > closed5["open"].iloc[-1]:
                    score_long += 5; reasons_long.append(f"volume {vr:.1f}x")
                else:
                    score_short += 5; reasons_short.append(f"volume {vr:.1f}x")

        funding = float(self.market.get("funding", 0.0))
        if funding > 0.0005:
            score_short += 3; reasons_short.append("positive funding")
        elif funding < -0.0005:
            score_long += 3; reasons_long.append("negative funding")

        direction = "LONG" if score_long > score_short else "SHORT"
        score = max(score_long, score_short)

        if not sweep and not fvg and not ob:
            return None

        if score < 70:
            return None

        if direction == "LONG":
            anchor = sweep["extreme"] if sweep else float(closed5["low"].iloc[-1])
            sl = anchor - 0.8 * atr5
            risk = price - sl
            if risk <= 0:
                return None
            tp1, tp2, tp3 = price + risk*1.0, price + risk*2.0, price + risk*3.0
            notes = " | ".join(reasons_long)
            setup = sweep["pool"] + " SWEEP" if sweep and sweep["direction"] == "LONG" else "SMC CONFLUENCE"
        else:
            anchor = sweep["extreme"] if sweep else float(closed5["high"].iloc[-1])
            sl = anchor + 0.8 * atr5
            risk = sl - price
            if risk <= 0:
                return None
            tp1, tp2, tp3 = price - risk*1.0, price - risk*2.0, price - risk*3.0
            notes = " | ".join(reasons_short)
            setup = sweep["pool"] + " SWEEP" if sweep and sweep["direction"] == "SHORT" else "SMC CONFLUENCE"

        key = f"{closed5['open_time'].iloc[-1]}-{direction}-{setup}"
        if key == self.last_signal_key:
            return None

        return Signal(
            created_at=now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
            direction=direction,
            setup=setup,
            score=int(score),
            entry=price,
            sl=float(sl),
            tp1=float(tp1),
            tp2=float(tp2),
            tp3=float(tp3),
            atr=float(atr5),
            notes=notes,
        ), key

    def scan(self):
        with LOCK:
            try:
                self.fetch()
                result = self.build_signal()
                if result:
                    signal, key = result
                    self.last_signal_key = key
                    self.last_signal = signal
                    if self.active is None:
                        self.active = signal
                        db_insert_trade(signal)

                        emoji = "🟢" if signal.direction == "LONG" else "🔴"
                        telegram(
                            f"{emoji} <b>BTCUSDT {signal.direction} SIGNAL</b>\n"
                            f"Score: <b>{signal.score}/100</b>\n"
                            f"Setup: <b>{signal.setup}</b>\n"
                            f"Entry: <code>{fmt_price(signal.entry)}</code>\n"
                            f"SL: <code>{fmt_price(signal.sl)}</code>\n"
                            f"TP1: <code>{fmt_price(signal.tp1)}</code>\n"
                            f"TP2: <code>{fmt_price(signal.tp2)}</code>\n"
                            f"TP3: <code>{fmt_price(signal.tp3)}</code>\n"
                            f"ATR: {signal.atr:.2f}\n\n"
                            f"Confluence: <i>{signal.notes}</i>"
                        )
            except Exception as e:
                self.last_error = str(e)

    def check_active(self):
        with LOCK:
            if not self.active:
                return
            try:
                price = BinanceClient().ticker()
                t = self.active

                if t.direction == "LONG":
                    if price <= t.sl:
                        db_close_trade(price, "SL HIT", -1.0)
                        telegram(f"🛑 <b>LONG SL HIT</b> @ {fmt_price(price)} (-1.0R)")
                        self.active = None
                    elif price >= t.tp3:
                        db_close_trade(price, "TP3 HIT 🔥", 3.0)
                        telegram(f"🎯 <b>LONG TP3 HIT 🔥</b> @ {fmt_price(price)} (+3.0R Target Complete)")
                        self.active = None
                    elif price >= t.tp2 and not getattr(t, 'hit_tp2', False):
                        setattr(t, 'hit_tp2', True)
                        telegram(f"✨ <b>LONG TP2 REACHED</b> @ {fmt_price(price)} (+2.0R)")
                    elif price >= t.tp1 and not getattr(t, 'hit_tp1', False):
                        setattr(t, 'hit_tp1', True)
                        telegram(f"🔹 <b>LONG TP1 REACHED</b> @ {fmt_price(price)} (+1.0R)")

                elif t.direction == "SHORT":
                    if price >= t.sl:
                        db_close_trade(price, "SL HIT", -1.0)
                        telegram(f"🛑 <b>SHORT SL HIT</b> @ {fmt_price(price)} (-1.0R)")
                        self.active = None
                    elif price <= t.tp3:
                        db_close_trade(price, "TP3 HIT 🔥", 3.0)
                        telegram(f"🎯 <b>SHORT TP3 HIT 🔥</b> @ {fmt_price(price)} (+3.0R Target Complete)")
                        self.active = None
                    elif price <= t.tp2 and not getattr(t, 'hit_tp2', False):
                        setattr(t, 'hit_tp2', True)
                        telegram(f"✨ <b>SHORT TP2 REACHED</b> @ {fmt_price(price)} (+2.0R)")
                    elif price <= t.tp1 and not getattr(t, 'hit_tp1', False):
                        setattr(t, 'hit_tp1', True)
                        telegram(f"🔹 <b>SHORT TP1 REACHED</b> @ {fmt_price(price)} (+1.0R)")
            except Exception as e:
                self.last_error = str(e)


# ----------------------------- 24/7 BACKGROUND WORKER -----------------------------

db_init()

if "engine" not in st.session_state:
    st.session_state.engine = SMCSignalEngine()

ENGINE = st.session_state.engine

def background_loop():
    while True:
        try:
            ENGINE.scan()
            for _ in range(12):
                ENGINE.check_active()
                time.sleep(5)
        except Exception:
            time.sleep(10)

if "bg_thread_started" not in st.session_state:
    st.session_state.bg_thread_started = True
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()


# ----------------------------- DASHBOARD UI -----------------------------

st.markdown("""
    <style>
        .block-container { padding-top: 1rem; padding-bottom: 2rem; }
        .stMetric { background-color: #0e1117; border: 1px solid #1f2937; padding: 10px; border-radius: 6px; }
    </style>
""", unsafe_allow_html=True)

st.title("⚡ BTCUSDT MULTI-TIER SMC RADAR")

with LOCK:
    current_price = ENGINE.market.get("price", None)
    funding_rate = ENGINE.market.get("funding", 0.0)
    oi = ENGINE.market.get("oi", 0.0)
    active_trade = ENGINE.active

# 1. Metric Row
c1, c2, c3, c4 = st.columns(4)
c1.metric("BTC PRICE", fmt_price(current_price))
c2.metric("FUNDING RATE", f"{funding_rate*100:.4f}%" if funding_rate else "--")
c3.metric("OPEN INTEREST", f"{oi:,.0f} BTC" if oi else "--")
c4.metric("RADAR STATUS", "ACTIVE TRADE" if active_trade else "SCANNING ALL TF", 
          delta="IN POSITION" if active_trade else "ARMED", delta_color="normal" if active_trade else "off")

# 2. Active Trade Box (if active)
if active_trade:
    st.info(
        f"🚨 **ACTIVE {active_trade.direction}** | Setup: **{active_trade.setup}** | Score: **{active_trade.score}/100**\n\n"
        f"**Entry:** {fmt_price(active_trade.entry)} | **SL:** {fmt_price(active_trade.sl)} | "
        f"**TP1 (1R):** {fmt_price(active_trade.tp1)} | **TP2 (2R):** {fmt_price(active_trade.tp2)} | **TP3 (3R):** {fmt_price(active_trade.tp3)}"
    )

# 3. Interactive Candlestick Chart (Plotly)
if "5m" in ENGINE.data and not ENGINE.data["5m"].empty:
    df_plot = ENGINE.data["5m"].tail(120).copy()
    fig = go.Figure(data=[go.Candlestick(
        x=df_plot["open_time"],
        open=df_plot["open"],
        high=df_plot["high"],
        low=df_plot["low"],
        close=df_plot["close"],
        name="BTCUSDT 5M"
    )])

    # Multi-Timeframe Liquidity Lines
    levs = liquidity_levels(ENGINE.data["5m"])
    
    # 48H Macro Liquidity
    if "48H_HIGH" in levs:
        fig.add_hline(y=levs["48H_HIGH"], line_dash="dash", line_color="#ef4444", annotation_text="48H HIGH POOL (BSL)")
    if "48H_LOW" in levs:
        fig.add_hline(y=levs["48H_LOW"], line_dash="dash", line_color="#10b981", annotation_text="48H LOW POOL (SSL)")
        
    # 1H Intermediate Liquidity
    if "1H_HIGH" in levs:
        fig.add_hline(y=levs["1H_HIGH"], line_dash="dot", line_color="#f59e0b", annotation_text="1H HIGH")
    if "1H_LOW" in levs:
        fig.add_hline(y=levs["1H_LOW"], line_dash="dot", line_color="#06b6d4", annotation_text="1H LOW")

    # 5M Local Micro Liquidity
    if "5M_HIGH" in levs:
        fig.add_hline(y=levs["5M_HIGH"], line_dash="dot", line_color="#ec4899", line_width=1, annotation_text="5M PREV HIGH")
    if "5M_LOW" in levs:
        fig.add_hline(y=levs["5M_LOW"], line_dash="dot", line_color="#a855f7", line_width=1, annotation_text="5M PREV LOW")

    # Active Trade Overlay
    if active_trade:
        fig.add_hline(y=active_trade.entry, line_color="#38bdf8", annotation_text="ENTRY")
        fig.add_hline(y=active_trade.sl, line_dash="dash", line_color="#f43f5e", annotation_text="SL")
        fig.add_hline(y=active_trade.tp3, line_dash="dash", line_color="#22c55e", annotation_text="TP3")

    fig.update_layout(
        template="plotly_dark",
        height=480,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_rangeslider_visible=False,
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("Fetching market candle streams from Binance Futures...")

# 4. Vault Database History
st.subheader("📜 SMC Signal & Trade Vault")
rows = db_rows()
if rows:
    df_vault = pd.DataFrame(rows, columns=[
        "Time", "Setup", "Direction", "Score", "Entry", "SL", "TP1", "TP2", "TP3",
        "Exit", "Result", "PnL (R)", "Confluence Notes"
    ])
    st.dataframe(df_vault, use_container_width=True)
else:
    st.write("Vault empty. Scanning institutional setups across 48H, 1H, and 5M...")
