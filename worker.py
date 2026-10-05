import os
import time
import math
import sqlite3
import logging
import traceback
import threading
from datetime import datetime, timezone
import requests
from flask import Flask

# ============================================================
# BTCUSDT SMC INSTITUTIONAL V2 ENGINE (PRODUCTION GRADE)
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__)

@app.route("/")
@app.route("/healthz")
def health():
    return "BTCUSDT Institutional SMC V2 Running 24/7", 200

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

NORMAL_MIN = 70
NORMAL_MAX = 84
EVENT_MIN = 85
EVENT_COOLDOWN = 12 * 60 * 60  # 12 Hours cooldown for rare events
BASE = "https://fapi.binance.com"

LOCK = threading.RLock()

# ---------------- 1. PERSISTENT DATABASE ----------------
def db():
    c = sqlite3.connect(DB_FILE, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, created_at TEXT, signal_type TEXT, tf TEXT, direction TEXT,
        setup TEXT, score INTEGER, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        exit REAL, result TEXT, pnl_r REAL, confluence TEXT, status TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS event_locks(
        event_key TEXT PRIMARY KEY, created_at TEXT, direction TEXT,
        score INTEGER, status TEXT)""")
    c.commit(); c.close()

init_db()

# ---------------- 2. TELEGRAM CLIENT ----------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logging.error(f"Telegram dispatch failed: {e}")

def db_upsert_trade(t, status="OPEN", exit_p=0.0, res="RUNNING", pnl=0.0):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, confluence, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], t["score"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], exit_p, res, pnl, ", ".join(t.get("reasons", [])), status))
        c.commit(); c.close()
    except Exception as e:
        logging.error(f"DB Upsert Error: {e}")

# ---------------- 3. ROBUST BINANCE FETCHER ----------------
def futures_get(path, params=None, timeout=4):
    try:
        r = requests.get(BASE + path, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning(f"Binance API fetch fail [{path}]: {e}")
        return None

def fetch_klines(interval, limit=300):
    raw = futures_get("/fapi/v1/klines", {"symbol": SYMBOL, "interval": interval, "limit": limit})
    if not isinstance(raw, list):
        return []
    # All completed candles except live forming one
    return [{
        "time": int(b[0] // 1000), "open": float(b[1]), "high": float(b[2]),
        "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])
    } for b in raw[:-1]]

def fetch_price():
    x = futures_get("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    try: return float(x["price"])
    except Exception: return 0.0

def fetch_funding_rate():
    try:
        x = futures_get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
        return float(x.get("lastFundingRate", 0.0))
    except Exception:
        return 0.0

def fetch_oi_hist():
    try:
        # Fetches last 5 periods of 15m Open Interest
        raw = futures_get("/futures/data/openInterestHist", {"symbol": SYMBOL, "period": "15m", "limit": 5})
        if isinstance(raw, list) and len(raw) >= 2:
            cur = float(raw[-1]["sumOpenInterest"])
            prev = float(raw[-2]["sumOpenInterest"])
            return ((cur - prev) / prev) if prev > 0 else 0.0
    except Exception:
        pass
    return 0.0

# ---------------- 4. QUANT & SMC INDICATORS ----------------
def calculate_rsi(candles, period=14):
    if len(candles) < period + 2:
        return 50.0
    closes = [c["close"] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr(candles, period=14):
    if len(candles) < period + 1:
        return 120.0
    trs = [max(candles[i]["high"] - candles[i]["low"], 
               abs(candles[i]["high"] - candles[i-1]["close"]), 
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

def volume_ratio(candles, period=20):
    if len(candles) < period + 1: return 1.0
    avg = sum(x["vol"] for x in candles[-period - 1:-1]) / period
    return (candles[-1]["vol"] / avg) if avg > 0 else 1.0

# --- SMC STRUCTURE: SWINGS, BOS, AND CHOCH ---
def confirmed_swings(candles, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h, l = candles[i]["high"], candles[i]["low"]
        if all(h > candles[j]["high"] for j in range(i-left, i)) and all(h >= candles[j]["high"] for j in range(i+1, i+right+1)):
            highs.append((i, h))
        if all(l < candles[j]["low"] for j in range(i-left, i)) and all(l <= candles[j]["low"] for j in range(i+1, i+right+1)):
            lows.append((i, l))
    return highs, lows

def smc_structure(candles):
    if len(candles) < 25:
        return {"bias": "NEUTRAL", "bos": None, "choch": None, "last_high": None, "last_low": None}
    
    highs, lows = confirmed_swings(candles)
    if not highs or not lows:
        return {"bias": "NEUTRAL", "bos": None, "choch": None, "last_high": None, "last_low": None}
    
    last_h = highs[-1][1]
    last_l = lows[-1][1]
    close = candles[-1]["close"]

    # Trend Bias from latest 2 swing sequences
    bias = "NEUTRAL"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]: bias = "BULL"
        elif highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]: bias = "BEAR"

    # BOS = Break with current trend
    # CHoCH = Break opposing prior trend swing point
    bos, choch = None, None
    if bias == "BULL":
        if close > last_h: bos = "LONG"
        if close < last_l: choch = "SHORT"
    elif bias == "BEAR":
        if close < last_l: bos = "SHORT"
        if close > last_h: choch = "LONG"
    else:
        if close > last_h: bos = "LONG"
        elif close < last_l: bos = "SHORT"

    return {"bias": bias, "bos": bos, "choch": choch, "last_high": last_h, "last_low": last_l}

def liquidity_sweep(candles, lookback=10):
    if len(candles) < lookback + 2: return None
    ref = candles[-lookback-1:-1]
    cur = candles[-1]
    hi, lo = max(x["high"] for x in ref), min(x["low"] for x in ref)
    if cur["low"] < lo and cur["close"] > lo: return {"dir": "LONG", "level": lo}
    if cur["high"] > hi and cur["close"] < hi: return {"dir": "SHORT", "level": hi}
    return None

def detect_institutional_fvg(candles):
    """3-Candle Institutional Imbalance"""
    if len(candles) < 3: return None
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    # Bullish FVG: C3 low > C1 high
    if c3["low"] > c1["high"]:
        gap = c3["low"] - c1["high"]
        if gap > 15.0: # Meaningful gap buffer
            return {"dir": "LONG", "low": c1["high"], "high": c3["low"]}
    # Bearish FVG: C3 high < C1 low
    if c3["high"] < c1["low"]:
        gap = c1["low"] - c3["high"]
        if gap > 15.0:
            return {"dir": "SHORT", "low": c3["high"], "high": c1["low"]}
    return None

# --- 105-POINT INSTITUTIONAL SCORING ---
def evaluate_score(direction, d):
    pts = 0
    why = []
    if d.get("sweep") == direction:
        pts += 20; why.append("Liquidity Sweep (+20)")
    if d.get("htf_bias") == ("BULL" if direction == "LONG" else "BEAR"):
        pts += 15; why.append("HTF Bias Alignment (+15)")
    if d.get("structure") == direction:
        pts += 15; why.append("BOS / CHoCH Trigger (+15)")
    if d.get("fvg") == direction:
        pts += 10; why.append("Institutional FVG (+10)")
    if d.get("displacement"):
        pts += 10; why.append("Strong Displacement (+10)")
    if d.get("volume", 1.0) >= 1.25:
        pts += 10; why.append("Volume Expansion (+10)")
    if d.get("oi_change", 0.0) >= 0.005: # > 0.5% OI growth
        pts += 10; why.append("Real 15M OI Inflow (+10)")
    if d.get("funding_ok"):
        pts += 8; why.append("Contrarian Funding (+8)")
    if d.get("rsi_ok"):
        pts += 7; why.append("RSI Momentum Zone (+7)")

    return min(105, pts), why

# ---------------- 5. 24/7 BACKGROUND WORKER ENGINE ----------------
class AutonomousEngine:
    def __init__(self):
        self.normal = {"15M": None, "1H": None, "4H": None}
        self.event = None
        self.event_lock_until = 0
        self.last_signal_time = {"15M": 0, "1H": 0, "4H": 0}
        self.recover_state()

    def recover_state(self):
        """Restore active running trades from SQLite on restart"""
        try:
            c = db()
            rows = c.execute("SELECT trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, status FROM trades WHERE status='OPEN'").fetchall()
            c.close()
            for r in rows:
                t = {
                    "id": r[0], "created": r[1], "type": r[2], "tf": r[3], "dir": r[4],
                    "setup": r[5], "score": r[6], "entry": r[7], "sl": r[8],
                    "tp1": r[9], "tp2": r[10], "tp3": r[11],
                    "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False,
                    "reasons": []
                }
                if t["type"] == "EVENT": self.event = t
                else: self.normal[t["tf"]] = t
            logging.info(f"State recovery complete. Recovered active trades: {len(rows)}")
        except Exception as e:
            logging.error(f"State recovery error: {e}")

    def manage_positions(self, p, s4):
        """Multi-Tier Exit: TP1 Partial + BE -> TP2 Partial + Trail -> TP3 Full Target"""
        with LOCK:
            # 1. Normal Trades
            for tf, t in list(self.normal.items()):
                if not t: continue
                # LONG
                if t["dir"] == "LONG":
                    # TP1 Hit
                    if p >= t["tp1"] and not t["tp1_hit"]:
                        t["tp1_hit"] = True
                        t["sl"] = t["entry"]  # Shift to Break-Even
                        t["be"] = True
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP1 HIT (+1R)</b>\nBooked 33% profit. 🛡 SL moved to <b>Break-Even (${t['entry']:,.2f})</b>")
                    
                    # TP2 Hit
                    elif p >= t["tp2"] and not t["tp2_hit"]:
                        t["tp2_hit"] = True
                        t["sl"] = t["tp1"]  # Lock profit at TP1
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP2 HIT (+2R)</b>\nBooked 33% profit. 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")

                    # Exit Trigger
                    if p <= t["sl"]:
                        res = "BE EXIT" if t["be"] else "SL HIT"
                        pnl = 0.5 if t["tp1_hit"] else -1.0
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[{tf} NORMAL] CLOSED: {res}</b> @ ${p:,.2f} (Net: {pnl:+.1f}R)")
                        self.normal[tf] = None

                    elif p >= t["tp3"]:
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", pnl=3.0)
                        send_telegram(f"🔥 <b>[{tf} NORMAL] FULL TP3 TARGET REACHED!</b> @ ${p:,.2f} (+3.0R)")
                        self.normal[tf] = None

                # SHORT
                elif t["dir"] == "SHORT":
                    if p <= t["tp1"] and not t["tp1_hit"]:
                        t["tp1_hit"] = True
                        t["sl"] = t["entry"]
                        t["be"] = True
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP1 HIT (+1R)</b>\nBooked 33% profit. 🛡 SL moved to <b>Break-Even (${t['entry']:,.2f})</b>")

                    elif p <= t["tp2"] and not t["tp2_hit"]:
                        t["tp2_hit"] = True
                        t["sl"] = t["tp1"]
                        db_upsert_trade(t, status="OPEN")
                        send_telegram(f"🎯 <b>[{tf} NORMAL] TP2 HIT (+2R)</b>\nBooked 33% profit. 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")

                    if p >= t["sl"]:
                        res = "BE EXIT" if t["be"] else "SL HIT"
                        pnl = 0.5 if t["tp1_hit"] else -1.0
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[{tf} NORMAL] CLOSED: {res}</b> @ ${p:,.2f} (Net: {pnl:+.1f}R)")
                        self.normal[tf] = None

                    elif p <= t["tp3"]:
                        db_upsert_trade(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", pnl=3.0)
                        send_telegram(f"🔥 <b>[{tf} NORMAL] FULL TP3 TARGET REACHED!</b> @ ${p:,.2f} (+3.0R)")
                        self.normal[tf] = None

            # 2. Rare Event Position
            if self.event:
                ev = self.event
                if ev["dir"] == "LONG":
                    if p >= ev["tp1"] and not ev["be"]:
                        ev["be"] = True
                        ev["sl"] = ev["entry"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP1 (+1R) REACHED</b>\nSL secured at BE (${ev['entry']:,.2f})")
                    
                    if p >= ev["tp3"] and not ev.get("runner_active"):
                        ev["runner_active"] = True
                        send_telegram(f"🔥 <b>[EVENT] TP3 HIT (+5R)</b>\nPartials banked! Trailing runner behind 4H Swing Lows.")

                    if ev.get("runner_active") and s4.get("last_low") and s4["last_low"] > ev["sl"]:
                        ev["sl"] = s4["last_low"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"🛡 <b>[EVENT 4H TRAIL]</b> SL raised to ${ev['sl']:,.2f}")

                    if p <= ev["sl"]:
                        res = "4H TRAIL EXIT" if ev.get("runner_active") else ("BE EXIT" if ev["be"] else "EVENT SL HIT")
                        pnl = 4.5 if ev.get("runner_active") else (0.0 if ev["be"] else -1.0)
                        db_upsert_trade(ev, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[EVENT FINISHED] {res}</b> @ ${p:,.2f} (Net: {pnl:+.1f}R)")
                        self.event = None

                elif ev["dir"] == "SHORT":
                    if p <= ev["tp1"] and not ev["be"]:
                        ev["be"] = True
                        ev["sl"] = ev["entry"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"⚡ <b>[EVENT] TP1 (+1R) REACHED</b>\nSL secured at BE (${ev['entry']:,.2f})")

                    if p <= ev["tp3"] and not ev.get("runner_active"):
                        ev["runner_active"] = True
                        send_telegram(f"🔥 <b>[EVENT] TP3 HIT (+5R)</b>\nPartials banked! Trailing runner behind 4H Swing Highs.")

                    if ev.get("runner_active") and s4.get("last_high") and s4["last_high"] < ev["sl"]:
                        ev["sl"] = s4["last_high"]
                        db_upsert_trade(ev, status="OPEN")
                        send_telegram(f"🛡 <b>[EVENT 4H TRAIL]</b> SL lowered to ${ev['sl']:,.2f}")

                    if p >= ev["sl"]:
                        res = "4H TRAIL EXIT" if ev.get("runner_active") else ("BE EXIT" if ev["be"] else "EVENT SL HIT")
                        pnl = 4.5 if ev.get("runner_active") else (0.0 if ev["be"] else -1.0)
                        db_upsert_trade(ev, status="CLOSED", exit_p=p, res=res, pnl=pnl)
                        send_telegram(f"🏁 <b>[EVENT FINISHED] {res}</b> @ ${p:,.2f} (Net: {pnl:+.1f}R)")
                        self.event = None

    def scan_market(self, candles_map, p, funding_rate, oi_delta):
        with LOCK:
            c15 = candles_map.get("15m", [])
            c5 = candles_map.get("5m", [])
            c1h = candles_map.get("1h", [])
            c4 = candles_map.get("4h", [])
            cd = candles_map.get("1d", [])

            if min(len(c15), len(c5), len(c1h), len(c4)) < 30:
                return

            cur = c15[-1]
            if cur["time"] == self.last_signal_time["15M"]:
                return

            s15 = smc_structure(c15)
            s5 = smc_structure(c5)
            s1h = smc_structure(c1h)
            s4 = smc_structure(c4)
            sd = smc_structure(cd) if len(cd) > 20 else {}

            a15 = atr(c15)
            vr15 = volume_ratio(c15)
            rsi15 = calculate_rsi(c15)
            disp = abs(cur["close"] - cur["open"]) > (1.1 * a15)

            # Context parameters
            sw15 = liquidity_sweep(c15, 6)
            sw48 = liquidity_sweep(c15, 192) # 48H range sweep
            fvg15 = detect_institutional_fvg(c15)

            # Determine potential signal direction
            cand_dir = None
            setup_name = ""
            if sw15:
                cand_dir = sw15["dir"]
                setup_name = "15M Liquidity Sweep + Structure Shift"
            elif s15["choch"]:
                cand_dir = s15["choch"]
                setup_name = "15M CHoCH Market Structure Shift"
            elif fvg15:
                cand_dir = fvg15["dir"]
                setup_name = "Institutional FVG Imbalance Fill"

            if not cand_dir:
                return

            # Real Filters
            funding_ok = (cand_dir == "LONG" and funding_rate <= 0.0001) or (cand_dir == "SHORT" and funding_rate >= -0.0001)
            rsi_ok = (cand_dir == "LONG" and 35 <= rsi15 <= 60) or (cand_dir == "SHORT" and 40 <= rsi15 <= 65)
            struct_dir = s15["choch"] or s15["bos"] or s5["bos"]

            score, reasons = evaluate_score(cand_dir, {
                "sweep": sw15["dir"] if sw15 else None,
                "htf_bias": s1h["bias"],
                "structure": struct_dir,
                "fvg": fvg15["dir"] if fvg15 else None,
                "displacement": disp,
                "volume": vr15,
                "oi_change": oi_delta,
                "funding_ok": funding_ok,
                "rsi_ok": rsi_ok
            })

            # ---------------- A. EVENT STREAM (Score 85+) ----------------
            if score >= EVENT_MIN and sw48 and self.event is None and time.time() >= self.event_lock_until:
                sl = (cur["low"] - 1.5 * a15) if cand_dir == "LONG" else (cur["high"] + 1.5 * a15)
                risk = abs(cur["close"] - sl)
                tp1 = cur["close"] + risk if cand_dir == "LONG" else cur["close"] - risk
                tp2 = cur["close"] + (2 * risk) if cand_dir == "LONG" else cur["close"] - (2 * risk)
                tp3 = cur["close"] + (5 * risk) if cand_dir == "LONG" else cur["close"] - (5 * risk)

                self.event = {
                    "id": f"EVT_{int(time.time()*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "type": "EVENT", "tf": "48H/HTF", "dir": cand_dir, "setup": "48H Sweep + Multi-TF MTF Confluence",
                    "score": score, "entry": cur["close"], "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False,
                    "reasons": reasons
                }
                self.event_lock_until = time.time() + EVENT_COOLDOWN
                self.last_signal_time["15M"] = cur["time"]
                db_upsert_trade(self.event, status="OPEN")

                send_telegram(
                    f"⚡ <b>BTCUSDT RARE EVENT SIGNAL</b>\n\n"
                    f"<b>Type:</b> INSTITUTIONAL EVENT\n<b>Direction:</b> {cand_dir}\n"
                    f"<b>Setup:</b> 48H Sweep + HTF Alignment\n\n"
                    f"🔹 <b>Entry:</b> ${cur['close']:,.2f}\n🛑 <b>SL:</b> ${sl:,.2f}\n"
                    f"🎯 <b>TP1 (+1R):</b> ${tp1:,.2f}\n🎯 <b>TP2 (+2R):</b> ${tp2:,.2f}\n🔥 <b>TP3 (+5R):</b> ${tp3:,.2f}\n\n"
                    f"📊 <b>Score:</b> {score}/105\n💡 <b>Confluence:</b> {', '.join(reasons)}\n"
                    f"⏳ <i>4H Structure Trailing Protected</i>"
                )

            # ---------------- B. NORMAL STREAM (Score 70–84) ----------------
            elif NORMAL_MIN <= score <= NORMAL_MAX and self.normal["15M"] is None:
                sl = (cur["low"] - 1.2 * a15) if cand_dir == "LONG" else (cur["high"] + 1.2 * a15)
                risk = abs(cur["close"] - sl)
                tp1 = cur["close"] + risk if cand_dir == "LONG" else cur["close"] - risk
                tp2 = cur["close"] + (2 * risk) if cand_dir == "LONG" else cur["close"] - (2 * risk)
                tp3 = cur["close"] + (3 * risk) if cand_dir == "LONG" else cur["close"] - (3 * risk)

                self.normal["15M"] = {
                    "id": f"NRM_{int(time.time()*1000)}", "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "type": "NORMAL", "tf": "15M", "dir": cand_dir, "setup": setup_name,
                    "score": score, "entry": cur["close"], "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "tp1_hit": False, "tp2_hit": False, "be": False, "runner_active": False,
                    "reasons": reasons
                }
                self.last_signal_time["15M"] = cur["time"]
                db_upsert_trade(self.normal["15M"], status="OPEN")

                emoji = "🟢" if cand_dir == "LONG" else "🔴"
                send_telegram(
                    f"{emoji} <b>BTCUSDT SMC ENTRY SIGNAL [15M]</b>\n\n"
                    f"<b>Direction:</b> {cand_dir}\n<b>Setup:</b> {setup_name}\n\n"
                    f"🔹 <b>Entry:</b> ${cur['close']:,.2f}\n🛑 <b>SL:</b> ${sl:,.2f}\n"
                    f"🎯 <b>TP1 (+1R):</b> ${tp1:,.2f}\n🎯 <b>TP2 (+2R):</b> ${tp2:,.2f}\n🎯 <b>TP3 (+3R):</b> ${tp3:,.2f}\n\n"
                    f"📊 <b>Score:</b> {score}/105\n💡 <b>Confluence:</b> {', '.join(reasons)}\n"
                    f"🛡 <i>TP1 = Move to BE | TP2 = Trail to TP1</i>"
                )

# ---------------- 6. WORKER DAEMON EXECUTION ----------------
def run_worker_thread():
    time.sleep(2)
    logging.info("🚀 Production SMC V2 Worker Initialized...")
    send_telegram("🚀 <b>BTCUSDT Institutional V2 Worker Live</b>\nDual-Stream SMC Engine Active 24/7.")

    engine = AutonomousEngine()
    candles_map = {}
    last_pull = 0
    last_meta_pull = 0
    funding = 0.0
    oi_delta = 0.0

    while True:
        try:
            p = fetch_price()
            now = time.time()

            # Multi-TF Klines Sync
            if now - last_pull >= 15:
                for tf in ["5m", "15m", "1h", "4h", "1d"]:
                    k = fetch_klines(tf, 250)
                    if k: candles_map[tf] = k
                last_pull = now

            # Derivatives Data Sync
            if now - last_meta_pull >= 45:
                funding = fetch_funding_rate()
                oi_delta = fetch_oi_hist()
                last_meta_pull = now

            # Position Monitoring
            if p > 10000:
                c4 = candles_map.get("4h", [])
                s4 = smc_structure(c4) if len(c4) > 20 else {}
                engine.manage_positions(p, s4)

                # Signal Evaluation
                engine.scan_market(candles_map, p, funding, oi_delta)

            time.sleep(3)
        except Exception as e:
            logging.error(f"Worker Engine Exception: {e}\n{traceback.format_exc()}")
            time.sleep(5)

# Spawn worker thread
threading.Thread(target=run_worker_thread, daemon=True).start()

# Flask entrypoint for Render
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
