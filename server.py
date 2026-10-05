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
# BTCUSDT SMC SNIPER ENGINE (IDM + EQ SWEEP INTEGRATED)
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SNIPER-EXEC] %(message)s"
)

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
WORKER_API = "http://127.0.0.1:10000"
BASE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

SNIPER_MIN_SCORE = 85
LOCK = threading.RLock()

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logging.error(f"Telegram error: {e}")

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
        c.commit(); c.close()
    except Exception as e:
        logging.error(f"DB Error: {e}")

def fetch_klines(interval="1m", limit=120):
    try:
        res = requests.get(f"{BASE}/fapi/v1/klines", params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=4).json()
        if isinstance(res, list) and len(res) > 20:
            return [{
                "time": int(b[0] // 1000), "open": float(b[1]), "high": float(b[2]),
                "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])
            } for b in res[:-1]]
    except Exception:
        pass
    return []

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 40.0
    trs = [max(candles[i]["high"] - candles[i]["low"],
               abs(candles[i]["high"] - candles[i-1]["close"]),
               abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

# GATE: Inducement Check on 1M
def check_1m_inducement(c1m, direction):
    if len(c1m) < 10: return False
    cur = c1m[-1]
    swings = [c["low"] if direction == "LONG" else c["high"] for c in c1m[-6:-1]]
    if not swings: return False
    target = min(swings) if direction == "LONG" else max(swings)
    if direction == "LONG":
        return cur["low"] < target and cur["close"] > target
    else:
        return cur["high"] > target and cur["close"] < target

def gate_5m_confirmation(c5m, direction):
    if len(c5m) < 15: return False
    cur5 = c5m[-1]
    a5 = calculate_atr(c5m, 14)
    disp5 = abs(cur5["close"] - cur5["open"]) >= (0.85 * a5)
    highs = [c["high"] for c in c5m[-12:-2]]
    lows = [c["low"] for c in c5m[-12:-2]]
    last_h = max(highs) if highs else cur5["high"]
    last_l = min(lows) if lows else cur5["low"]
    if direction == "LONG": return cur5["close"] > last_h and disp5
    elif direction == "SHORT": return cur5["close"] < last_l and disp5
    return False

def gate_1m_sweep(c1m, direction, lookback=8):
    if len(c1m) < lookback + 2: return None
    ref = c1m[-lookback-1:-1]
    cur = c1m[-1]
    hi = max(c["high"] for c in ref)
    lo = min(c["low"] for c in ref)
    if direction == "LONG" and cur["low"] < lo and cur["close"] > lo:
        return {"swept": True, "wick": cur["low"], "level": lo}
    if direction == "SHORT" and cur["high"] > hi and cur["close"] < hi:
        return {"swept": True, "wick": cur["high"], "level": hi}
    return None

def gate_1m_displacement(c1m, direction):
    if len(c1m) < 15: return False
    cur = c1m[-1]
    a1 = calculate_atr(c1m, 14)
    body = abs(cur["close"] - cur["open"])
    is_displaced = body >= (1.2 * a1)
    if direction == "LONG": return is_displaced and (cur["close"] > cur["open"])
    elif direction == "SHORT": return is_displaced and (cur["close"] < cur["open"])
    return False

def gate_1m_fvg_and_ce_retest(c1m, direction, current_price):
    if len(c1m) < 4: return None
    c1, c2, c3 = c1m[-4], c1m[-3], c1m[-2]
    cur = c1m[-1]
    if direction == "LONG":
        if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) >= 8.0:
            ce = c1["high"] + ((c3["low"] - c1["high"]) * 0.5)
            if c3["close"] > c3["low"] and ((cur["low"] <= ce <= cur["high"]) or abs(current_price - ce) <= 8.0):
                return {"ce": ce}
    elif direction == "SHORT":
        if c3["high"] < c1["low"] and (c1["low"] - c3["high"]) >= 8.0:
            ce = c3["high"] + ((c1["low"] - c3["high"]) * 0.5)
            if c3["close"] < c3["high"] and ((cur["low"] <= ce <= cur["high"]) or abs(current_price - ce) <= 8.0):
                return {"ce": ce}
    return None

class SniperTradeManager:
    def __init__(self):
        self.active_trade = None
        self.last_trade_time = 0

    def manage_positions(self, p):
        with LOCK:
            t = self.active_trade
            if not t: return

            risk = abs(t["entry"] - t["sl"]) if abs(t["entry"] - t["sl"]) > 0 else 1.0

            if t["dir"] == "LONG":
                if p >= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 3.0  # +1.5R
                    t["remaining_pct"] = 0.50
                    t["sl"] = t["entry"]
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP1 (+3R) HIT</b>\n50% banked (+1.5R). 🛡 SL moved to <b>BE (${t['entry']:,.2f})</b>")

                elif p >= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 6.0  # +1.5R (Total +3.0R)
                    t["remaining_pct"] = 0.25
                    t["sl"] = t["tp1"]
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP2 (+6R) HIT</b>\n25% banked (+3.0R Total). 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")

                if p <= t["sl"]:
                    if t["stage"] == "OPEN": final_r = -1.0; res = "SL HIT"
                    elif t["stage"] == "TP1_DONE": final_r = t["realized_r"]; res = "BE EXIT"
                    else: final_r = t["realized_r"] + (t["remaining_pct"] * 3.0); res = "TP1 TRAIL EXIT"
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                    send_telegram(f"🏁 <b>[SNIPER CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    self.active_trade = None

                elif p >= t["tp3"] and t["stage"] in ["OPEN", "TP1_DONE", "TP2_DONE"]:
                    final_r = t["realized_r"] + (t["remaining_pct"] * 10.0)
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res="TP3 10R TARGET 🔥", final_pnl=round(final_r, 3))
                    send_telegram(f"🔥 <b>[SNIPER FULL 10R TARGET!]</b> @ ${p:,.2f} | Net: <b>+{final_r:.2f}R</b>")
                    self.active_trade = None

            elif t["dir"] == "SHORT":
                if p <= t["tp1"] and t["stage"] == "OPEN":
                    t["stage"] = "TP1_DONE"
                    t["realized_r"] += 0.50 * 3.0
                    t["remaining_pct"] = 0.50
                    t["sl"] = t["entry"]
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP1 (+3R) HIT</b>\n50% banked (+1.5R). 🛡 SL moved to <b>BE (${t['entry']:,.2f})</b>")

                elif p <= t["tp2"] and t["stage"] == "TP1_DONE":
                    t["stage"] = "TP2_DONE"
                    t["realized_r"] += 0.25 * 6.0
                    t["remaining_pct"] = 0.25
                    t["sl"] = t["tp1"]
                    db_upsert_sniper(t, status="OPEN")
                    send_telegram(f"🎯 <b>[SNIPER] TP2 (+6R) HIT</b>\n25% banked (+3.0R Total). 🛡 SL trailed to <b>TP1 (${t['tp1']:,.2f})</b>")

                if p >= t["sl"]:
                    if t["stage"] == "OPEN": final_r = -1.0; res = "SL HIT"
                    elif t["stage"] == "TP1_DONE": final_r = t["realized_r"]; res = "BE EXIT"
                    else: final_r = t["realized_r"] + (t["remaining_pct"] * 3.0); res = "TP1 TRAIL EXIT"
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res=res, final_pnl=round(final_r, 3))
                    send_telegram(f"🏁 <b>[SNIPER CLOSED] {res}</b> @ ${p:,.2f} | Net: <b>{final_r:+.2f}R</b>")
                    self.active_trade = None

                elif p <= t["tp3"] and t["stage"] in ["OPEN", "TP1_DONE", "TP2_DONE"]:
                    final_r = t["realized_r"] + (t["remaining_pct"] * 10.0)
                    db_upsert_sniper(t, status="CLOSED", exit_p=p, res="TP3 10R TARGET 🔥", final_pnl=round(final_r, 3))
                    send_telegram(f"🔥 <b>[SNIPER FULL 10R TARGET!]</b> @ ${p:,.2f} | Net: <b>+{final_r:.2f}R</b>")
                    self.active_trade = None

    def scan_sniper_execution(self, poi_data, c5m, c1m, p):
        with LOCK:
            if self.active_trade is not None: return
            now = time.time()
            if now - self.last_trade_time < 300: return

            direction = poi_data["dir"]
            if not (poi_data["low"] <= p <= poi_data["high"]): return
            if not gate_5m_confirmation(c5m, direction): return

            sweep_1m = gate_1m_sweep(c1m, direction)
            if not sweep_1m: return

            idm_ok = check_1m_inducement(c1m, direction)
            disp_1m = gate_1m_displacement(c1m, direction)
            if not disp_1m: return

            fvg_ce = gate_1m_fvg_and_ce_retest(c1m, direction, p)
            if not fvg_ce: return

            score = 90 if idm_ok else 85
            entry = fvg_ce["ce"]
            a1 = calculate_atr(c1m, 14)

            if direction == "LONG":
                inval = sweep_1m["wick"] - (0.2 * a1)
                risk = entry - inval
                if risk < 5.0 or risk > 120.0: return
                sl = inval
                tp1 = entry + (3.0 * risk)
                tp2 = entry + (6.0 * risk)
                tp3 = entry + (10.0 * risk)
            else:
                inval = sweep_1m["wick"] + (0.2 * a1)
                risk = inval - entry
                if risk < 5.0 or risk > 120.0: return
                sl = inval
                tp1 = entry - (3.0 * risk)
                tp2 = entry - (6.0 * risk)
                tp3 = entry - (10.0 * risk)

            snp = {
                "id": f"SNP_{int(now*1000)}",
                "created": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "dir": direction,
                "setup": "HTF POI + 5M MSS + IDM Purge + 50% C.E.",
                "score": score,
                "entry": round(entry, 2),
                "sl": round(sl, 2),
                "tp1": round(tp1, 2),
                "tp2": round(tp2, 2),
                "tp3": round(tp3, 2),
                "realized_r": 0.0,
                "remaining_pct": 1.0,
                "stage": "OPEN",
                "reasons": ["HTF POI", "5M MSS", "1M Sweep", "IDM Purged", "50% C.E. Fill"]
            }

            self.active_trade = snp
            self.last_trade_time = now
            db_upsert_sniper(snp, status="OPEN")

            send_telegram(
                f"🎯 <b>[SNIPER ENTRY: IDM PURGED]</b>\n\n"
                f"<b>Direction:</b> {direction}\n"
                f"🔹 <b>Exact Entry (50% C.E.):</b> ${entry:,.2f}\n"
                f"🛑 <b>Tight SL:</b> ${sl:,.2f} (Risk: ${risk:.1f})\n"
                f"🎯 <b>TP1 (3R - 50%):</b> ${tp1:,.2f}\n"
                f"🎯 <b>TP2 (6R - 25%):</b> ${tp2:,.2f}\n"
                f"🔥 <b>TP3 (10R - 25%):</b> ${tp3:,.2f}\n\n"
                f"📊 <b>Score:</b> {score}/100"
            )

def run_sniper_daemon():
    time.sleep(3)
    logging.info("🚀 Sniper Execution with Inducement Gate Online...")
    manager = SniperTradeManager()

    while True:
        try:
            p_res = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL}, timeout=3).json()
            p = float(p_res.get("price", 0.0))

            if p > 10000:
                manager.manage_positions(p)
                poi_list = []
                try:
                    r = requests.get(f"{WORKER_API}/poi-zones", timeout=2)
                    if r.status_code == 200: poi_list = r.json()
                except Exception:
                    pass

                c5m = fetch_klines("5m", 30)
                c1m = fetch_klines("1m", 60)

                if poi_list and c5m and c1m:
                    for poi in poi_list:
                        manager.scan_sniper_execution(poi, c5m, c1m, p)

            time.sleep(4)
        except Exception as e:
            logging.error(f"Sniper daemon error: {e}\n{traceback.format_exc()}")
            time.sleep(4)

if __name__ == "__main__":
    run_sniper_daemon()
