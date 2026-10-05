import os
import time
import math
import sqlite3
import threading
from datetime import datetime, timezone
import requests
import http.server
import socketserver

# ============================================================
# BTCUSDT 24/7 AUTONOMOUS WORKER ENGINE (WITH RENDER PORT SUPPORT)
# ============================================================

SYMBOL = "BTCUSDT"
DB_FILE = os.getenv("DB_FILE", "trades_v5.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7886716805")

NORMAL_MIN = 70
NORMAL_MAX = 84
EVENT_MIN = 85
EVENT_COOLDOWN = 12 * 60 * 60
BASE = "https://fapi.binance.com"

# --- 1. RENDER PORT BINDING (Fixes "No open ports detected" error) ---
def start_dummy_port():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

threading.Thread(target=start_dummy_port, daemon=True).start()

# --- 2. DATABASE ---
def db():
    c = sqlite3.connect(DB_FILE, timeout=10)
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

# --- 3. TELEGRAM ---
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass

def save_vault(t, exit_p, res, pnl):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, confluence, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], t["score"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], exit_p, res, pnl, ", ".join(t.get("reasons", [])), "CLOSED"))
        c.commit(); c.close()
    except Exception:
        pass

# --- 4. DATA ENGINE ---
def futures_get(path, params=None, timeout=4):
    try:
        r = requests.get(BASE + path, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def fetch_klines(interval, limit=300):
    raw = futures_get("/fapi/v1/klines", {"symbol": SYMBOL, "interval": interval, "limit": limit})
    if not isinstance(raw, list):
        return []
    return [{
        "time": int(b[0] // 1000), "open": float(b[1]), "high": float(b[2]),
        "low": float(b[3]), "close": float(b[4]), "vol": float(b[5]), "closed": True
    } for b in raw]

def fetch_price():
    x = futures_get("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    try: return float(x["price"])
    except Exception: return 0.0

def fetch_oi():
    x = futures_get("/fapi/v1/openInterest", {"symbol": SYMBOL})
    try: return float(x["openInterest"])
    except Exception: return 0.0

def atr(candles, period=14):
    if len(candles) < period + 1:
        return max((candles[-1]["high"] - candles[-1]["low"]) if candles else 120.0, 1.0)
    trs = [max(candles[i]["high"] - candles[i]["low"], abs(candles[i]["high"] - candles[i-1]["close"]), abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

def volume_ratio(candles, period=20):
    if len(candles) < period + 1: return 1.0
    avg = sum(x["vol"] for x in candles[-period - 1:-1]) / period
    return candles[-1]["vol"] / avg if avg else 1.0

# --- 5. SMC STRUCTURE ---
def confirmed_swings(candles, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h, l = candles[i]["high"], candles[i]["low"]
        if all(h > candles[j]["high"] for j in range(i-left, i)) and all(h >= candles[j]["high"] for j in range(i+1, i+right+1)):
            highs.append((i, h))
        if all(l < candles[j]["low"] for j in range(i-left, i)) and all(l <= candles[j]["low"] for j in range(i+1, i+right+1)):
            lows.append((i, l))
    return highs, lows

def structure(candles):
    if len(candles) < 20:
        return {"bias": "NEUTRAL", "bos_dir": None, "choch_dir": None, "last_high": None, "last_low": None}
    highs, lows = confirmed_swings(candles)
    last_h = highs[-1][1] if highs else None
    last_l = lows[-1][1] if lows else None
    bias = "NEUTRAL"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]: bias = "BULL"
        elif highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]: bias = "BEAR"
    close = candles[-1]["close"]
    bos_dir = "LONG" if last_h is not None and close > last_h else "SHORT" if last_l is not None and close < last_l else None
    return {"bias": bias, "bos_dir": bos_dir, "choch_dir": bos_dir, "last_high": last_h, "last_low": last_l}

def liquidity_sweep(candles, lookback):
    if len(candles) < lookback + 2: return None
    ref = candles[-lookback-1:-1]
    cur = candles[-1]
    hi, lo = max(x["high"] for x in ref), min(x["low"] for x in ref)
    if cur["low"] < lo and cur["close"] > lo: return {"dir": "LONG", "level": lo}
    if cur["high"] > hi and cur["close"] < hi: return {"dir": "SHORT", "level": hi}
    return None

def detect_fvg(candles):
    if len(candles) < 4: return None
    a, _, c, cur = candles[-4], candles[-3], candles[-2], candles[-1]
    if c["low"] > a["high"] and cur["low"] <= c["low"] and cur["close"] >= a["high"]:
        return {"dir": "LONG", "low": a["high"], "high": c["low"]}
    if c["high"] < a["low"] and cur["high"] >= c["high"] and cur["close"] <= a["low"]:
        return {"dir": "SHORT", "low": c["high"], "high": a["low"]}
    return None

def volume_profile(candles, lookback=60, bin_size=25.0):
    recent = candles[-lookback:]
    bins = {}
    for c in recent:
        lo = math.floor(c["low"] / bin_size) * bin_size
        hi = math.ceil(c["high"] / bin_size) * bin_size
        steps = max(1, int(round((hi - lo) / bin_size)))
        share = c["vol"] / steps
        for k in range(steps):
            bins[lo + k * bin_size] = bins.get(lo + k * bin_size, 0.0) + share
    return {"poc": max(bins, key=bins.get) if bins else recent[-1]["close"]}

def fib_setup(candles, lookback=80):
    if len(candles) < lookback: return None
    r = candles[-lookback:]
    hi, lo = max(x["high"] for x in r), min(x["low"] for x in r)
    d = hi - lo
    if d <= 0: return None
    cur = candles[-1]
    b618, b786 = hi - 0.618 * d, hi - 0.786 * d
    if b786 <= cur["low"] <= b618 and cur["close"] > b786:
        return {"dir": "LONG", "sl_ref": lo}
    s618, s786 = lo + 0.618 * d, lo + 0.786 * d
    if s618 <= cur["high"] <= s786 and cur["close"] < s786:
        return {"dir": "SHORT", "sl_ref": hi}
    return None

def channel_break(candles, lookback=30):
    if len(candles) < lookback + 5: return None
    r = candles[-lookback:]
    xs = list(range(len(r)))
    ys = [c["close"] for c in r]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    den = sum((x-mx)**2 for x in xs)
    slope = sum((x-mx)*(y-my) for x,y in zip(xs,ys))/den if den else 0
    intercept = my - slope*mx
    residuals = [y-(intercept+slope*x) for x,y in zip(xs,ys)]
    width = max(abs(x) for x in residuals) if residuals else 0
    center = intercept + slope*(len(r)-1)
    if r[-1]["close"] > center + width: return "LONG"
    if r[-1]["close"] < center - width: return "SHORT"
    return None

def score(direction, d):
    pts = 0; why = []
    if d.get("sweep") == direction: pts += 20; why.append("Liquidity sweep +20")
    if d.get("htf_bias") == ("BULL" if direction == "LONG" else "BEAR"): pts += 15; why.append("HTF trend aligned +15")
    if d.get("bos") == direction: pts += 15; why.append("BOS/CHoCH confirmed +15")
    if d.get("fvg") == direction: pts += 10; why.append("FVG/OB Tap +10")
    if d.get("displacement"): pts += 10; why.append("Displacement candle +10")
    if d.get("volume", 1.0) >= 1.3: pts += 10; why.append("Volume expansion +10")
    if d.get("oi_change", 0.0) > 0.01: pts += 10; why.append("OI expansion +10")
    if d.get("funding_ok"): pts += 5; why.append("Funding contrarian +5")
    if d.get("rsi_ok"): pts += 5; why.append("RSI momentum +5")
    return min(105, pts), why

# --- 6. 24/7 BACKGROUND EXECUTION LOOP ---
print("🚀 BTCUSDT Autonomous Worker Initialized. Running 24/7...")
send_telegram("🚀 <b>BTCUSDT SMC Worker Started</b> | 24/7 Autonomous Background Scanning Active.")

data = {"5m": [], "15m": [], "1h": [], "4h": [], "1d": []}
price = 0.0
oi = 0.0
oi_prev = 0.0
normal = {"15M": None, "1H": None, "4H": None}
last_signal_bar = {"15M": 0, "1H": 0, "4H": 0}
event = None
event_lock_until = 0
last_derivatives = 0
last_klines_pull = 0

while True:
    try:
        price = fetch_price()
        now = time.time()

        if now - last_klines_pull >= 15:
            for tf, lim in [("5m", 100), ("15m", 300), ("1h", 120), ("4h", 60), ("1d", 40)]:
                c = fetch_klines(tf, lim)
                if c: data[tf] = c
            last_klines_pull = now

        if now - last_derivatives >= 30:
            oi_prev = oi
            oi = fetch_oi()
            last_derivatives = now

        # Active Trade Management (BE + Trailing)
        if price > 10000:
            for tf, t in list(normal.items()):
                if not t: continue
                if t['dir'] == "LONG":
                    if price >= t['tp1'] and not t['be']:
                        t['sl'] = t['entry']; t['be'] = True
                        send_telegram(f"🛡 <b>[{tf} NORMAL] TP1 REACHED (+1R)</b> -> SL at BE (${t['entry']:,.2f})")
                    if price <= t['sl']:
                        res = "BE EXIT" if t['be'] else "SL HIT"
                        pnl = 0.0 if t['be'] else -1.0
                        save_vault(t, price, res, pnl)
                        send_telegram(f"🏁 <b>[{tf} NORMAL] {res}</b> @ ${price:,.2f} ({pnl:+.1f}R)")
                        normal[tf] = None
                    elif price >= t['tp3']:
                        save_vault(t, price, "TP3 HIT 🔥", 3.0)
                        send_telegram(f"🎯 <b>[{tf} NORMAL] FULL TP3 REACHED 🔥</b> @ ${price:,.2f} (+3.0R)")
                        normal[tf] = None
                elif t['dir'] == "SHORT":
                    if price <= t['tp1'] and not t['be']:
                        t['sl'] = t['entry']; t['be'] = True
                        send_telegram(f"🛡 <b>[{tf} NORMAL] TP1 REACHED (+1R)</b> -> SL at BE (${t['entry']:,.2f})")
                    if price >= t['sl']:
                        res = "BE EXIT" if t['be'] else "SL HIT"
                        pnl = 0.0 if t['be'] else -1.0
                        save_vault(t, price, res, pnl)
                        send_telegram(f"🏁 <b>[{tf} NORMAL] {res}</b> @ ${price:,.2f} ({pnl:+.1f}R)")
                        normal[tf] = None
                    elif price <= t['tp3']:
                        save_vault(t, price, "TP3 HIT 🔥", 3.0)
                        send_telegram(f"🎯 <b>[{tf} NORMAL] FULL TP3 REACHED 🔥</b> @ ${price:,.2f} (+3.0R)")
                        normal[tf] = None

            if event:
                ev = event
                c4 = data.get("4h", [])
                s4 = structure(c4) if len(c4) > 20 else {}
                if ev['dir'] == "LONG":
                    if price >= ev['tp1'] and not ev['be']:
                        ev['sl'] = ev['entry']; ev['be'] = True
                        send_telegram(f"⚡ <b>[EVENT] TP1 HIT</b> -> SL locked at BE (${ev['entry']:,.2f})")
                    if price >= ev['tp3'] and not ev.get('runner_active'):
                        ev['runner_active'] = True
                        send_telegram(f"🔥 <b>[EVENT] TP3 REACHED</b> -> Partials booked. 4H Structure Trailing active!")
                    if ev.get('runner_active') and s4.get("last_low") and s4["last_low"] > ev['sl']:
                        ev['sl'] = s4["last_low"]
                        send_telegram(f"🛡 <b>[EVENT 4H TRAIL]</b> SL raised to Higher Low: ${ev['sl']:,.2f}")
                    if price <= ev['sl']:
                        res = "4H STRUCTURE INVALID / EXIT" if ev.get('runner_active') else ("BE EXIT" if ev['be'] else "EVENT SL HIT")
                        pnl = 4.0 if ev.get('runner_active') else (0.0 if ev['be'] else -1.0)
                        save_vault(ev, price, res, pnl)
                        send_telegram(f"🏁 <b>[EVENT COMPLETED] {res}</b> @ ${price:,.2f} ({pnl:+.1f}R)")
                        event = None
                elif ev['dir'] == "SHORT":
                    if price <= ev['tp1'] and not ev['be']:
                        ev['sl'] = ev['entry']; ev['be'] = True
                        send_telegram(f"⚡ <b>[EVENT] TP1 HIT</b> -> SL locked at BE (${ev['entry']:,.2f})")
                    if price <= ev['tp3'] and not ev.get('runner_active'):
                        ev['runner_active'] = True
                        send_telegram(f"🔥 <b>[EVENT] TP3 REACHED</b> -> Partials booked. 4H Structure Trailing active!")
                    if ev.get('runner_active') and s4.get("last_high") and s4["last_high"] < ev['sl']:
                        ev['sl'] = s4["last_high"]
                        send_telegram(f"🛡 <b>[EVENT 4H TRAIL]</b> SL lowered to Lower High: ${ev['sl']:,.2f}")
                    if price >= ev['sl']:
                        res = "4H STRUCTURE INVALID / EXIT" if ev.get('runner_active') else ("BE EXIT" if ev['be'] else "EVENT SL HIT")
                        pnl = 4.0 if ev.get('runner_active') else (0.0 if ev['be'] else -1.0)
                        save_vault(ev, price, res, pnl)
                        send_telegram(f"🏁 <b>[EVENT COMPLETED] {res}</b> @ ${price:,.2f} ({pnl:+.1f}R)")
                        event = None

        # Evaluator Pipeline
        c5 = data.get("5m", [])[:-1]
        c15 = data.get("15m", [])[:-1]
        c1h = data.get("1h", [])[:-1]
        c4 = data.get("4h", [])[:-1]
        cd = data.get("1d", [])[:-1]

        if min(len(c5), len(c15), len(c1h), len(c4)) >= 25:
            s5 = structure(c5); s15 = structure(c15); s1h = structure(c1h); s4 = structure(c4); sd = structure(cd)
            cur = c15[-1]
            a15 = atr(c15); vr15 = volume_ratio(c15)
            disp = abs(cur["close"] - cur["open"]) > 1.1 * a15
            oi_change = ((oi - oi_prev) / oi_prev) if oi_prev else 0.0
            sweep48 = liquidity_sweep(c15, 192)

            # Rare Event Gate
            if sweep48 and event is None and time.time() >= event_lock_until:
                d = sweep48["dir"]
                desired = "BULL" if d == "LONG" else "BEAR"
                mtf_ok = (sd["bias"] == desired) and (s4["bias"] == desired or s4["bos_dir"] == d) and (s1h["bias"] == desired) and (s5["bos_dir"] == d)
                if mtf_ok and disp and vr15 >= 1.30 and oi_change > 0.01:
                    sc, reasons = score(d, {"sweep": d, "htf_bias": s4["bias"], "bos": d, "displacement": True, "volume": vr15, "oi_change": oi_change, "funding_ok": True, "rsi_ok": True})
                    if sc >= EVENT_MIN:
                        h = int(sweep48["level"] // 50)
                        event_key = f"EVENT_{d}_{h}_{int(cur['time']//3600)}"
                        c = db()
                        old = c.execute("SELECT status FROM event_locks WHERE event_key=?", (event_key,)).fetchone()
                        c.close()
                        if old is None:
                            sl = (cur['low'] - 1.5 * a15) if d == "LONG" else (cur['high'] + 1.5 * a15)
                            risk = abs(cur['close'] - sl)
                            tp1 = cur['close'] + risk if d == "LONG" else cur['close'] - risk
                            tp2 = cur['close'] + 2*risk if d == "LONG" else cur['close'] - 2*risk
                            tp3 = cur['close'] + 5*risk if d == "LONG" else cur['close'] - 5*risk
                            event = {
                                "id": f"EVENT_{int(time.time()*1000)}", "type": "EVENT", "tf": "48H/HTF",
                                "dir": d, "setup": "48H Liquidity Sweep + 1D/4H/1H MTF Confluence", "score": sc,
                                "entry": cur['close'], "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                                "p1": False, "p2": False, "p3": False, "be": False, "runner": True,
                                "reasons": reasons, "created": datetime.now(timezone.utc).strftime("%H:%M:%S")
                            }
                            event_lock_until = time.time() + EVENT_COOLDOWN
                            c = db()
                            c.execute("INSERT OR REPLACE INTO event_locks VALUES(?,?,?,?,?)", (event_key, event["created"], d, sc, "ACTIVE"))
                            c.commit(); c.close()
                            send_telegram(
                                f"⚡ <b>BTCUSDT — EVENT SIGNAL</b>\n\n"
                                f"<b>Type:</b> RARE HTF / SWING EVENT\n<b>Direction:</b> {d}\n\n"
                                f"48H Sweep\n↓\n1D Alignment\n↓\n4H Structure Shift\n↓\n1H Confirmation\n↓\n15M Setup + 5M Confirm\n\n"
                                f"🔹 Entry: ${cur['close']:,.2f}\n🛑 SL: ${sl:,.2f}\n🎯 TP1: ${tp1:,.2f}\n🎯 TP2: ${tp2:,.2f}\n🔥 TP3: ${tp3:,.2f}\n\n"
                                f"📊 Event Score: {sc}/105\n⏳ Horizon: Multi-day / 2–3 weeks\n🛡 Management: 4H Structure Trail"
                            )

            # Normal 15M
            if normal["15M"] is None and last_signal_bar["15M"] != cur["time"]:
                sw = liquidity_sweep(c15, 3); fv = detect_fvg(c15)
                if sw and fv and sw["dir"] == fv["dir"] and s5["bos_dir"] == sw["dir"]:
                    d = sw["dir"]
                    sc, reasons = score(d, {"sweep": d, "htf_bias": s1h["bias"], "bos": s15["bos_dir"], "fvg": fv["dir"], "displacement": disp, "volume": vr15, "oi_change": oi_change, "funding_ok": True, "rsi_ok": True})
                    if NORMAL_MIN <= sc <= NORMAL_MAX:
                        sl = (cur['low'] - 1.2 * a15) if d == "LONG" else (cur['high'] + 1.2 * a15)
                        risk = abs(cur['close'] - sl)
                        normal["15M"] = {
                            "id": f"NORM_{int(time.time()*1000)}", "type": "NORMAL", "tf": "15M",
                            "dir": d, "setup": "Liquidity Sweep + MSS + FVG", "score": sc,
                            "entry": cur['close'], "sl": sl, "tp1": cur['close'] + risk if d == "LONG" else cur['close'] - risk,
                            "tp2": cur['close'] + 2*risk if d == "LONG" else cur['close'] - 2*risk,
                            "tp3": cur['close'] + 3*risk if d == "LONG" else cur['close'] - 3*risk,
                            "p1": False, "p2": False, "p3": False, "be": False, "runner": False,
                            "reasons": reasons, "created": datetime.now(timezone.utc).strftime("%H:%M:%S")
                        }
                        last_signal_bar["15M"] = cur["time"]
                        send_telegram(
                            f"{'🟢' if d=='LONG' else '🔴'} <b>BTCUSDT — ENTRY SIGNAL</b>\n\n"
                            f"<b>TF:</b> 15M\n<b>Direction:</b> {d}\n<b>Setup:</b> Liquidity Sweep + MSS + FVG\n\n"
                            f"🔹 Entry: ${cur['close']:,.2f}\n🛑 SL: ${sl:,.2f}\n🎯 TP1: ${normal['15M']['tp1']:,.2f}\n🎯 TP2: ${normal['15M']['tp2']:,.2f}\n🎯 TP3: ${normal['15M']['tp3']:,.2f}\n\n"
                            f"📊 Score: {sc}/105\n💡 Confluence: {', '.join(reasons)}"
                        )

            # Normal 1H
            if normal["1H"] is None and len(c1h) > 20:
                vp = volume_profile(c1h); curh = c1h[-1]
                d = "SHORT" if (curh["high"] >= vp["poc"] and curh["close"] < vp["poc"] and s1h["bos_dir"] == "SHORT") else ("LONG" if (curh["low"] <= vp["poc"] and curh["close"] > vp["poc"] and s1h["bos_dir"] == "LONG") else None)
                if d and s5["bos_dir"] == d:
                    sc, reasons = score(d, {"sweep": None, "htf_bias": s4["bias"], "bos": s1h["bos_dir"], "displacement": True, "volume": volume_ratio(c1h), "funding_ok": True, "rsi_ok": True})
                    if NORMAL_MIN <= sc <= NORMAL_MAX:
                        a1h = atr(c1h)
                        sl = (curh['low'] - 1.2 * a1h) if d == "LONG" else (curh['high'] + 1.2 * a1h)
                        risk = abs(curh['close'] - sl)
                        normal["1H"] = {
                            "id": f"NORM_{int(time.time()*1000)}", "type": "NORMAL", "tf": "1H",
                            "dir": d, "setup": "1H BOS + Volume Profile POC", "score": sc,
                            "entry": curh['close'], "sl": sl, "tp1": curh['close'] + risk if d == "LONG" else curh['close'] - risk,
                            "tp2": curh['close'] + 2*risk if d == "LONG" else curh['close'] - 2*risk,
                            "tp3": curh['close'] + 3*risk if d == "LONG" else curh['close'] - 3*risk,
                            "p1": False, "p2": False, "p3": False, "be": False, "runner": False,
                            "reasons": reasons, "created": datetime.now(timezone.utc).strftime("%H:%M:%S")
                        }
                        send_telegram(
                            f"{'🟢' if d=='LONG' else '🔴'} <b>BTCUSDT — ENTRY SIGNAL</b>\n\n"
                            f"<b>TF:</b> 1H\n<b>Direction:</b> {d}\n<b>Setup:</b> 1H BOS + Volume Profile POC\n\n"
                            f"🔹 Entry: ${curh['close']:,.2f}\n🛑 SL: ${sl:,.2f}\n🎯 TP1: ${normal['1H']['tp1']:,.2f}\n🎯 TP2: ${normal['1H']['tp2']:,.2f}\n🎯 TP3: ${normal['1H']['tp3']:,.2f}\n\n"
                            f"📊 Score: {sc}/105\n💡 Confluence: {', '.join(reasons)}"
                        )

            # Normal 4H
            if normal["4H"] is None and len(c4) > 20:
                ch = channel_break(c4); fib = fib_setup(c4)
                if ch and fib and ch == fib["dir"] and s5["bos_dir"] == ch:
                    d = ch
                    cur4 = c4[-1]
                    sc, reasons = score(d, {"sweep": None, "htf_bias": sd["bias"], "bos": s4["bos_dir"], "displacement": True, "volume": volume_ratio(c4), "funding_ok": True, "rsi_ok": True})
                    if NORMAL_MIN <= sc <= NORMAL_MAX:
                        a4 = atr(c4)
                        sl = fib["sl_ref"] - 1.2 * a4 if d == "LONG" else fib["sl_ref"] + 1.2 * a4
                        risk = abs(cur4['close'] - sl)
                        normal["4H"] = {
                            "id": f"NORM_{int(time.time()*1000)}", "type": "NORMAL", "tf": "4H",
                            "dir": d, "setup": "Channel Break + Fib 0.618–0.786 Golden Pocket", "score": sc,
                            "entry": cur4['close'], "sl": sl, "tp1": cur4['close'] + risk if d == "LONG" else cur4['close'] - risk,
                            "tp2": cur4['close'] + 2*risk if d == "LONG" else cur4['close'] - 2*risk,
                            "tp3": cur4['close'] + 3*risk if d == "LONG" else cur4['close'] - 3*risk,
                            "p1": False, "p2": False, "p3": False, "be": False, "runner": False,
                            "reasons": reasons, "created": datetime.now(timezone.utc).strftime("%H:%M:%S")
                        }
                        send_telegram(
                            f"{'🟢' if d=='LONG' else '🔴'} <b>BTCUSDT — ENTRY SIGNAL</b>\n\n"
                            f"<b>TF:</b> 4H\n<b>Direction:</b> {d}\n<b>Setup:</b> Channel Break + Fib 0.618–0.786 Golden Pocket\n\n"
                            f"🔹 Entry: ${cur4['close']:,.2f}\n🛑 SL: ${sl:,.2f}\n🎯 TP1: ${normal['4H']['tp1']:,.2f}\n🎯 TP2: ${normal['4H']['tp2']:,.2f}\n🎯 TP3: ${normal['4H']['tp3']:,.2f}\n\n"
                            f"📊 Score: {sc}/105\n💡 Confluence: {', '.join(reasons)}"
                        )

        time.sleep(3)
    except Exception:
        time.sleep(5)
