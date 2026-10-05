import os
import time
import math
import sqlite3
import logging
import traceback
import threading
from datetime import datetime, timezone
import requests

# ============================================================
# BTCUSDT SMC ULTRA-PRECISION SNIPER ENGINE (PRODUCTION SUITE)
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SNIPER] %(message)s"
)

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
BASE = "https://fapi.binance.com"

# Integrated Telegram Credentials
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

SNIPER_MIN_SCORE = 80
LOCK = threading.RLock()

# ---------------- 1. TELEGRAM DISPATCHER ----------------
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

# ---------------- 2. PERSISTENCE & DB HOOK ----------------
def db():
    c = sqlite3.connect(DB_FILE, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def db_upsert_sniper(t, status="OPEN", exit_p=0.0, res="RUNNING", final_pnl=0.0):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, realized_r, remaining_pct, stage, confluence, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], "SNIPER", "1M/5M", t["dir"], t["setup"], t["score"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], exit_p, res, final_pnl,
                   t.get("realized_r", 0.0), t.get("remaining_pct", 1.0), t.get("stage", "OPEN"),
                   ", ".join(t.get("reasons", [])), status))
        c.commit()
        c.close()
    except Exception as e:
        logging.error(f"DB Upsert Error: {e}")

# ---------------- 3. MARKET DATA UTILS ----------------
def fetch_klines(interval="1m", limit=120):
    try:
        url = f"{BASE}/fapi/v1/klines"
        res = requests.get(url, params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=4).json()
        if isinstance(res, list) and len(res) > 20:
            return [{
                "time": int(b[0] // 1000), "open": float(b[1]), "high": float(b[2]),
                "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])
            } for b in res[:-1]]
    except Exception as e:
        logging.warning(f"Klines fetch error [{interval}]: {e}")
    return []

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 40.0
    trs = [max(candles[i]["high"] - candles[i]["low"],
               abs(candles[i]["high"] - candles[i-1]["close"]),
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

# ---------------- 4. HTF POINT OF INTEREST (POI) ENGINE ----------------
def detect_htf_pois(candles_15m, candles_1h):
    """
    Scans 15M and 1H for unmitigated institutional imbalances & Order Blocks.
    """
    pois = []
    
    # 1. 15M Unmitigated Fair Value Gaps
    for i in range(len(candles_15m) - 3, max(0, len(candles_15m) - 20), -1):
        c1, c2, c3 = candles_15m[i-2], candles_15m[i-1], candles_15m[i]
        # Bullish FVG
        if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) > 20.0:
            top, bot = c3["low"], c1["high"]
            mitigated = any(c["low"] < bot for c in candles_15m[i+1:])
            if not mitigated:
                pois.append({"type": "15M_FVG", "dir": "LONG", "low": bot, "high": top})
        # Bearish FVG
        if c3["high"] < c1["low"] and (c1["low"] - c3["high"]) > 20.0:
            bot, top = c3["high"], c1["low"]
            mitigated = any(c["high"] > top for c in candles_15m[i+1:])
            if not mitigated:
                pois.append({"type": "15M_FVG", "dir": "SHORT", "low": bot, "high": top})

    # 2. 1H Order Blocks (Last opposing candle before displacement)
    for i in range(len(candles_1h) - 3, max(0, len(candles_1h) - 10), -1):
        c_ob = candles_1h[i-1]
        c_disp = candles_1h[i]
        if c_disp["close"] > c_disp["open"] and abs(c_disp["close"] - c_disp["open"]) > 150:
            pois.append({"type": "1H_OB", "dir": "LONG", "low": c_ob["low"], "high": c_ob["high"]})
        elif c_disp["close"] < c_disp["open"] and abs(c_disp["close"] - c_disp["open"]) > 150:
            pois.append({"type": "1H_OB", "dir": "SHORT", "low": c_ob["low"], "high": c_ob["high"]})

    return pois

# ---------------- 5. MICRO SMC & SNIPER SCORING ----------------
def detect_micro_sweep(candles, lookback=8):
    if len(candles) < lookback + 2: return None
    ref = candles[-lookback-1:-1]
    cur = candles[-1]
    hi = max(c["high"] for c in ref)
    lo = min(c["low"] for c in ref)

    if cur["low"] < lo and cur["close"] > lo:
        return {"dir": "LONG", "swept_level": lo, "wick_low": cur["low"]}
    if cur["high"] > hi and cur["close"] < hi:
        return {"dir": "SHORT", "swept_level": hi, "wick_high": cur["high"]}
    return None

def detect_micro_fvg(candles):
    if len(candles) < 3: return None
    c1, _, c3 = candles[-3], candles[-2], candles[-1]

    if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) > 10.0:
        ce = c1["high"] + ((c3["low"] - c1["high"]) * 0.5)
        return {"dir": "LONG", "low": c1["high"], "high": c3["low"], "ce": ce}

    if c3["high"] < c1["low"] and (c1["low"] - c3["high"]) > 10.0:
        ce = c3["high"] + ((c1["low"] - c3["high"]) * 0.5)
        return {"dir": "SHORT", "low": c3["high"], "high": c1["low"], "ce": ce}

    return None

def calculate_sniper_score(poi, sweep, is_displaced, has_fvg, vol_surge):
    score = 0
    reasons = []

    if poi:
        score += 25
        reasons.append(f"HTF POI Tap ({poi['type']}) [+25]")
    if sweep:
        score += 25
        reasons.append("Micro Liquidity Purge [+25]")
    if is_displaced:
        score += 20
        reasons.append("1M Explosive Displacement [+20]")
    if has_fvg:
        score += 15
        reasons.append("50% FVG C.E. Equilibrium [+15]")
    if vol_surge:
        score += 15
        reasons.append("LTF Volume Inflow [+15]")

    return min(100, score), reasons

# ---------------- 6. SNIPER MANAGER & EXECUTION CONTROLLER ----------------
class SniperExecutionEngine:
    def __init__(self):
        self.active_trade = None
        self.last_trade_time = 0
        self.recent_poi_locks = set()
        self.recover_state()

    def recover_state(self):
        try:
            c = db()
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM trades WHERE signal_type='SNIPER' AND status='OPEN' ORDER BY id DESC LIMIT 1").fetchone()
            c.close()
            if row:
                self.active_trade = {
                    "id": row["trade_id"], "created": row["created_at"], "dir": row["direction"],
                    "setup": row["setup"], "score": row["score"], "entry": row["entry"], "sl": row["sl"],
                    "tp1": row["tp1"], "tp2": row["tp2"], "tp3": row["tp3"],
                    "realized_r": row["realized_r"] or 0.0,
                    "remaining_pct": row["remaining_pct"] if row["remaining_pct"] is not None else 1.0,
                    "stage": row["stage"] or "OPEN",
                    "reasons": []
                }
                logging.info(f"Sniper State Recovered: {self.active_trade['id']}")
        except Exception as e:
            logging.error(f"Sniper recovery error: {e}")

    def manage_positions(self, p):
        with LOCK:
            t = self.active_trade
            if not t: return

            risk = abs(t["entry"] - t["sl"]) if abs(t["entry"] - t["sl"]) > 0 else 1.0

            # LONG SNIPER
            if t["dir"] == "LONG":
                # TP1 (+3R): Bank 50%, Move SL to BE
                if p >= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 3.0  # +1.50R
                    t["remaining_pct"] = 0.50
                    t["sl"] = t["entry"]           # BE
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP1 HIT (+3R)</b>\n50% secured (+1.50R). 🛡 SL shifted to <b>BE (${t['entry']:,.2f})</b>")

                # TP2 (+6R): Bank 25%, Trail SL to TP1 (+3R)
                elif p >= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 6.0  # +1.50R (Cumulative +3.0R)
                    t["remaining_pct"] = 0.25
                    t["sl"] = t["tp1"]             # Trail to TP1
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP2 HIT (+6R)</b>\n25% secured (+3.0R Total). 🛡 SL locked to <b>TP1 (${t['tp1']:,.2f})</b>")

                # Invalidation / Trail Trigger
                if p <= t["sl"]:
                    if t["stage"] == "OPEN": final_r = -1.0; res = "SNIPER SL HIT"
                    elif t["stage"] == "TP1_DONE": final_r = t["realized_r"]; res = "BE EXIT"
                    else: final_r = t["realized_r"] + (t["remaining_pct"] * 3.0); res = "TP1 TRAIL EXIT"
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                    send_telegram(f"🏁 <b>[SNIPER CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    self.active_trade = None

                # Full TP3 (+10R)
                elif p >= t["tp3"] and t["stage"] in ["OPEN", "TP1_DONE", "TP2_DONE"]:
                    final_r = t["realized_r"] + (t["remaining_pct"] * 10.0)  # Total +5.5R net
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", final_pnl=round(final_r, 3))
                    send_telegram(f"🔥 <b>[SNIPER 10R TARGET REACHED!]</b> @ ${p:,.2f} | Net: <b>+{final_r:.2f}R</b>")
                    self.active_trade = None

            # SHORT SNIPER
            elif t["dir"] == "SHORT":
                if p <= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 3.0
                    t["remaining_pct"] = 0.50
                    t["sl"] = t["entry"]
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP1 HIT (+3R)</b>\n50% secured (+1.50R). 🛡 SL shifted to <b>BE (${t['entry']:,.2f})</b>")

                elif p <= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 6.0
                    t["remaining_pct"] = 0.25
                    t["sl"] = t["tp1"]
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP2 HIT (+6R)</b>\n25% secured (+3.0R Total). 🛡 SL locked to <b>TP1 (${t['tp1']:,.2f})</b>")

                if p >= t["sl"]:
                    if t["stage"] == "OPEN": final_r = -1.0; res = "SNIPER SL HIT"
                    elif t["stage"] == "TP1_DONE": final_r = t["realized_r"]; res = "BE EXIT"
                    else: final_r = t["realized_r"] + (t["remaining_pct"] * 3.0); res = "TP1 TRAIL EXIT"
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                    send_telegram(f"🏁 <b>[SNIPER CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    self.active_trade = None

                elif p <= t["tp3"] and t["stage"] in ["OPEN", "TP1_DONE", "TP2_DONE"]:
                    final_r = t["realized_r"] + (t["remaining_pct"] * 10.0)
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res="TP3 FULL TARGET 🔥", final_pnl=round(final_r, 3))
                    send_telegram(f"🔥 <b>[SNIPER 10R TARGET REACHED!]</b> @ ${p:,.2f} | Net: <b>+{final_r:.2f}R</b>")
                    self.active_trade = None

    def scan_sniper(self, c1m, c5, c15, c1h, p):
        with LOCK:
            if self.active_trade is not None:
                return

            now = time.time()
            if now - self.last_trade_time < 300: # 5 Minutes anti-spam lock
                return

            pois = detect_htf_pois(c15, c1h)
            if not pois: return

            cur1 = c1m[-1]
            a1 = calculate_atr(c1m, 14)

            # Check matching active POI
            active_poi = None
            for poi in pois:
                if poi["low"] <= p <= poi["high"]:
                    active_poi = poi
                    break

            if not active_poi: return

            poi_dir = active_poi["dir"]
            poi_hash = f"{poi_dir}_{int(active_poi['low'])}_{int(active_poi['high'])}"
            if poi_hash in self.recent_poi_locks:
                return

            # Micro Sweeps
            sweep1 = detect_micro_sweep(c1m, 8)
            sweep5 = detect_micro_sweep(c5, 6)
            sweep = sweep1 or sweep5

            if not sweep or sweep["dir"] != poi_dir:
                return

            # Explosive 1M Displacement
            body = abs(cur1["close"] - cur1["open"])
            is_displaced = body >= (1.1 * a1)
            if not is_displaced: return

            # Micro FVG Consequent Encroachment (C.E.)
            micro_fvg = detect_micro_fvg(c1m)
            has_fvg = micro_fvg and micro_fvg["dir"] == poi_dir
            entry = micro_fvg["ce"] if has_fvg else cur1["close"]

            # Volume surge
            avg_vol = sum(c["vol"] for c in c1m[-10:-1]) / 9.0
            vol_surge = cur1["vol"] >= (1.25 * avg_vol)

            score, reasons = calculate_sniper_score(active_poi, sweep, is_displaced, has_fvg, vol_surge)
            if score < SNIPER_MIN_SCORE:
                return

            # Invalidation SL Calculation (Minimal Risk Envelope)
            if poi_dir == "LONG":
                invalidation = sweep.get("wick_low", cur1["low"]) - (0.2 * a1)
                risk = entry - invalidation
                if risk < 8.0 or risk > 150.0: return # Filter unrealistic extremes
                sl = invalidation
                tp1 = entry + (3.0 * risk)
                tp2 = entry + (6.0 * risk)
                tp3 = entry + (10.0 * risk)
            else:
                invalidation = sweep.get("wick_high", cur1["high"]) + (0.2 * a1)
                risk = invalidation - entry
                if risk < 8.0 or risk > 150.0: return
                sl = invalidation
                tp1 = entry - (3.0 * risk)
                tp2 = entry - (6.0 * risk)
                tp3 = entry - (10.0 * risk)

            sniper_trade = {
                "id": f"SNP_{int(now*1000)}",
                "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "dir": poi_dir,
                "setup": f"{active_poi['type']} Tap + 1M Sweep & C.E.",
                "score": score,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "realized_r": 0.0,
                "remaining_pct": 1.0,
                "stage": "OPEN",
                "reasons": reasons
            }

            self.active_trade = sniper_trade
            self.last_trade_time = now
            self.recent_poi_locks.add(poi_hash)
            db_upsert_sniper(sniper_trade, status="OPEN")

            emoji = "🟢" if poi_dir == "LONG" else "🔴"
            send_telegram(
                f"🎯 <b>BTCUSDT INSTITUTIONAL SNIPER ENTRY</b>\n\n"
                f"<b>Direction:</b> {poi_dir}\n"
                f"<b>Setup:</b> {sniper_trade['setup']}\n\n"
                f"🔹 <b>Entry:</b> ${entry:,.2f}\n"
                f"🛑 <b>Tight SL:</b> ${sl:,.2f} (Risk: ${risk:.1f})\n"
                f"🎯 <b>TP1 (1:3R):</b> ${tp1:,.2f}\n"
                f"🎯 <b>TP2 (1:6R):</b> ${tp2:,.2f}\n"
                f"🔥 <b>TP3 (1:10R):</b> ${tp3:,.2f}\n\n"
                f"📊 <b>Score:</b> {score}/100\n"
                f"💡 <b>Confluence:</b> {', '.join(reasons)}"
            )

# ---------------- 7. CONTINUOUS RUNNER DAEMON ----------------
def run_sniper_daemon():
    time.sleep(2)
    logging.info("🚀 Production Sniper Execution Daemon Live...")
    engine = SniperExecutionEngine()

    while True:
        try:
            p_res = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL}, timeout=3).json()
            p = float(p_res.get("price", 0.0))

            if p > 10000:
                engine.manage_positions(p)

                # Micro scans
                c1m = fetch_klines("1m", 60)
                c5m = fetch_klines("5m", 40)
                c15m = fetch_klines("15m", 30)
                c1h = fetch_klines("1h", 20)

                if min(len(c1m), len(c5m), len(c15m), len(c1h)) > 15:
                    engine.scan_sniper(c1m, c5m, c15m, c1h, p)

            time.sleep(5)
        except Exception as e:
            logging.error(f"Sniper daemon exception: {e}\n{traceback.format_exc()}")
            time.sleep(5)

if __name__ == "__main__":
    run_sniper_daemon()
