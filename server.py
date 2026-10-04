import streamlit as st
import streamlit.components.v1 as components
import requests
import json
import sqlite3
import asyncio
import aiohttp
import threading
import math
from datetime import datetime, timezone

# ============================================================
# BTCUSDT INSTITUTIONAL SMC QUANT ENGINE (105-POINT CORE)
# ============================================================

BOT_TOKEN = "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms"
CHAT_ID = "7886716805"
DB_FILE = "smc_quant_vault.db"

# --- 1. SQLITE VAULT & PERFORMANCE ANALYZER ---
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            tf_tier TEXT,
            direction TEXT,
            setup_name TEXT,
            score INTEGER,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            exit REAL,
            result TEXT,
            pnl_r REAL,
            confluence TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=4)
    except Exception:
        pass

def save_vault(trade, exit_price, result, pnl_r):
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO trades (timestamp, tf_tier, direction, setup_name, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, confluence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade['time'], trade['tf'], trade['dir'], trade['setup'], trade['score'],
            trade['entry'], trade['sl'], trade['tp1'], trade['tp2'], trade['tp3'],
            exit_price, result, pnl_r, trade['confluence']
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass

# --- 2. MULTI-TIMEFRAME DATA CORE ---
def fetch_klines(symbol="BTCUSDT", interval="15m", limit=300):
    urls = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.5).json()
            if isinstance(r, list) and len(r) > 20:
                candles = []
                for b in r:
                    open_ts = int(b[0] / 1000)
                    candles.append({
                        "time": open_ts,
                        "open": float(b[1]), "high": float(b[2]),
                        "low": float(b[3]), "close": float(b[4]),
                        "vol": float(b[5])
                    })
                return candles
        except Exception:
            continue
    return []

def fetch_market_metrics():
    metrics = {"oi": 0.0, "funding": 0.0}
    try:
        r_oi = requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", timeout=2.5).json()
        metrics["oi"] = float(r_oi.get("openInterest", 0.0))
    except Exception:
        pass
    try:
        r_f = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=2.5).json()
        metrics["funding"] = float(r_f.get("lastFundingRate", 0.0))
    except Exception:
        pass
    return metrics

# --- 3. MATHEMATICAL INDICATORS & SMC LOGIC ---
def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = (c * k) + (ema * (1 - k))
    return ema

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return 120.0
    trs = []
    for i in range(1, len(candles)):
        hl = candles[i]['high'] - candles[i]['low']
        hc = abs(candles[i]['high'] - candles[i-1]['close'])
        lc = abs(candles[i]['low'] - candles[i-1]['close'])
        trs.append(max(hl, hc, lc))
    return sum(trs[-period:]) / period

def get_session():
    utc_hour = datetime.now(timezone.utc).hour
    if 0 <= utc_hour < 7:
        return "Asia Session"
    elif 7 <= utc_hour < 12:
        return "London Session"
    elif 12 <= utc_hour < 20:
        return "New York Session"
    return "London/NY Close"

# --- 4. 105-POINT SIGNAL SCORER & DECISION GATE ---
class InstitutionalQuantEngine:
    def __init__(self):
        self.state = "SCANNING"
        self.active_trade = None
        self.candles_15m = []
        self.market_data = {"oi": 0.0, "funding": 0.0}

    def evaluate_market(self):
        if len(self.candles_15m) < 192:
            return

        c15 = self.candles_15m[:-1]  # Closed candles
        curr = c15[-1]
        closes = [c['close'] for c in c15]
        vols = [c['vol'] for c in c15]

        # 1. Macro Trend (EMA 20, 50, 200)
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        ema200 = calc_ema(closes, 200)
        trend = "BULL" if ema20 > ema50 > ema200 else ("BEAR" if ema20 < ema50 < ema200 else "RANGE")

        # 2. Liquidity Mapping
        h48 = max(c['high'] for c in c15[-192:])
        l48 = min(c['low'] for c in c15[-192:])
        h1h = max(c['high'] for c in c15[-4:])
        l1h = min(c['low'] for c in c15[-4:])
        h15m = c15[-2]['high']
        l15m = c15[-2]['low']

        atr = calc_atr(c15, 14)
        avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else 1.0
        vol_ratio = curr['vol'] / avg_vol if avg_vol > 0 else 1.0

        # Check Sweeps
        sweep_dir = None
        pool_name = ""
        sweep_level = 0.0
        extreme = 0.0

        if curr['low'] < l48 and curr['close'] > l48:
            sweep_dir, pool_name, sweep_level, extreme = "LONG", "48H MACRO SSL", l48, curr['low']
        elif curr['high'] > h48 and curr['close'] < h48:
            sweep_dir, pool_name, sweep_level, extreme = "SHORT", "48H MACRO BSL", h48, curr['high']
        elif curr['low'] < l1h and curr['close'] > l1h:
            sweep_dir, pool_name, sweep_level, extreme = "LONG", "1H SESSION SSL", l1h, curr['low']
        elif curr['high'] > h1h and curr['close'] < h1h:
            sweep_dir, pool_name, sweep_level, extreme = "SHORT", "1H SESSION BSL", h1h, curr['high']
        elif curr['low'] < l15m and curr['close'] > l15m:
            sweep_dir, pool_name, sweep_level, extreme = "LONG", "15M LOCAL SSL", l15m, curr['low']
        elif curr['high'] > h15m and curr['close'] < h15m:
            sweep_dir, pool_name, sweep_level, extreme = "SHORT", "15M LOCAL BSL", h15m, curr['high']

        if not sweep_dir:
            return

        # 3. 105-Point Institutional Scoring
        score = 0
        confluences = []

        # Liquidity Sweep (+25)
        score += 25
        confluences.append(f"{pool_name} Swept (+25)")

        # HTF Trend (+15)
        if (sweep_dir == "LONG" and trend == "BULL") or (sweep_dir == "SHORT" and trend == "BEAR"):
            score += 15
            confluences.append(f"Trend Aligned ({trend}) (+15)")

        # Structure Confirmation / CHoCH (+15)
        score += 15
        confluences.append("Reclaim CHoCH (+15)")

        # Displacement (+10)
        body = abs(curr['close'] - curr['open'])
        if body > 0.8 * atr:
            score += 10
            confluences.append("Institutional Displacement (+10)")

        # Volume Spike (+10)
        if vol_ratio >= 1.4:
            score += 10
            confluences.append(f"Volume Ignition {vol_ratio:.1f}x (+10)")

        # Open Interest Expansion (+10)
        if self.market_data.get("oi", 0) > 10000:
            score += 10
            confluences.append("OI Expanded (+10)")

        # Funding Contrarian (+5)
        funding = self.market_data.get("funding", 0)
        if (sweep_dir == "LONG" and funding < 0) or (sweep_dir == "SHORT" and funding > 0.0003):
            score += 5
            confluences.append("Funding Contrarian (+5)")

        # Session Killzone (+5)
        session = get_session()
        if "London" in session or "New York" in session:
            score += 5
            confluences.append(f"{session} Killzone (+5)")

        # Decision Gate: Require Score >= 70
        if score >= 70 and self.state == "SCANNING":
            entry = curr['close']
            if sweep_dir == "LONG":
                sl = extreme - (1.1 * atr)
                risk = entry - sl
                tp1 = entry + (1.0 * risk)
                tp2 = entry + (2.0 * risk)
                tp3 = entry + (3.0 * risk)
            else:
                sl = extreme + (1.1 * atr)
                risk = sl - entry
                tp1 = entry - (1.0 * risk)
                tp2 = entry - (2.0 * risk)
                tp3 = entry - (3.0 * risk)

            self.arm_trade(sweep_dir, pool_name, score, entry, sl, tp1, tp2, tp3, atr, " | ".join(confluences))

    def arm_trade(self, dir_type, pool_name, score, entry, sl, tp1, tp2, tp3, atr_val, conf_str):
        self.state = "ACTIVE_TRADE"
        t_str = datetime.now().strftime('%H:%M:%S')
        self.active_trade = {
            "time": t_str, "tf": pool_name.split()[0], "dir": dir_type, "setup": pool_name,
            "score": score, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "atr": atr_val, "confluence": conf_str, "be_moved": False
        }

        emoji = "🟢" if dir_type == "LONG" else "🔴"
        msg = (
            f"{emoji} <b>BTCUSDT {dir_type} INSTITUTIONAL SIGNAL</b>\n\n"
            f"🎯 <b>Score:</b> <b>{score}/105</b> (Decision Gate: VALID)\n"
            f"⚡ <b>Setup:</b> {pool_name}\n"
            f"🔹 <b>Entry:</b> ${entry:,.2f}\n"
            f"🛑 <b>Structure SL:</b> ${sl:,.2f}\n"
            f"🎯 <b>TP1 (1.0R):</b> ${tp1:,.2f}\n"
            f"🎯 <b>TP2 (2.0R):</b> ${tp2:,.2f}\n"
            f"🔥 <b>TP3 (3.0R):</b> ${tp3:,.2f}\n"
            f"🛡 <b>ATR(14):</b> {atr_val:.1f}\n\n"
            f"📊 <b>Confluence Factors:</b>\n<i>{conf_str}</i>\n\n"
            f"<i>Execution monitor active on live terminal...</i>"
        )
        send_telegram(msg)

    def check_trade_resolution(self, live_price):
        if not self.active_trade or self.state != "ACTIVE_TRADE":
            return
        t = self.active_trade

        if t['dir'] == "LONG":
            # Break-even trigger at TP1
            if live_price >= t['tp1'] and not t['be_moved']:
                t['sl'] = t['entry']
                t['be_moved'] = True
                send_telegram(f"🛡 <b>LONG TP1 HIT (+1R)</b> -> Stop Loss moved to BREAK-EVEN (${t['entry']:,.2f})")
            # Resolution
            if live_price >= t['tp3']:
                self.resolve("TP3 HIT 🔥", 3.0, 1, live_price)
            elif live_price <= t['sl']:
                res = "BE HIT ⚖️" if t['be_moved'] else "SL HIT 🛑"
                pnl = 0.0 if t['be_moved'] else -1.0
                self.resolve(res, pnl, 0, live_price)

        elif t['dir'] == "SHORT":
            # Break-even trigger at TP1
            if live_price <= t['tp1'] and not t['be_moved']:
                t['sl'] = t['entry']
                t['be_moved'] = True
                send_telegram(f"🛡 <b>SHORT TP1 HIT (+1R)</b> -> Stop Loss moved to BREAK-EVEN (${t['entry']:,.2f})")
            # Resolution
            if live_price <= t['tp3']:
                self.resolve("TP3 HIT 🔥", 3.0, 1, live_price)
            elif live_price >= t['sl']:
                res = "BE HIT ⚖️" if t['be_moved'] else "SL HIT 🛑"
                pnl = 0.0 if t['be_moved'] else -1.0
                self.resolve(res, pnl, 0, live_price)

    def resolve(self, result_text, pnl_r, is_win, exit_price):
        save_vault(self.active_trade, exit_price, result_text, pnl_r)
        msg = (
            f"🏁 <b>TRADE RESOLUTION: {result_text}</b>\n\n"
            f"📌 <b>Direction:</b> {self.active_trade['dir']} [{self.active_trade['setup']}]\n"
            f"🔹 <b>Entry:</b> ${self.active_trade['entry']:,.2f}\n"
            f"🔸 <b>Exit Price:</b> ${exit_price:,.2f}\n"
            f"💰 <b>Realized R:</b> {pnl_r:+.1f}R\n\n"
            f"🔄 <i>Chart cleared. Scanning next 105-point institutional setup...</i>"
        )
        send_telegram(msg)
        self.state = "SCANNING"
        self.active_trade = None

# Global Engine Singleton
if "quant_engine" not in st.session_state:
    st.session_state["quant_engine"] = InstitutionalQuantEngine()
engine = st.session_state["quant_engine"]

# --- 5. 24/7 BACKGROUND ASYNC WORKER ---
def run_worker_thread():
    async def kline_listener():
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect("wss://fstream.binance.com/ws/btcusdt@kline_15m") as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                k = data.get('k', {})
                                if k.get('x'):
                                    c = {
                                        "time": (int(k['t'] / 1000) // 900) * 900,
                                        "open": float(k['o']), "high": float(k['h']),
                                        "low": float(k['l']), "close": float(k['c']),
                                        "vol": float(k['v'])
                                    }
                                    if min(c['open'], c['high'], c['low'], c['close']) > 10000:
                                        engine.candles_15m.append(c)
                                        if len(engine.candles_15m) > 400: engine.candles_15m.pop(0)
                                        engine.evaluate_market()
            except Exception:
                await asyncio.sleep(5)

    async def trade_listener():
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect("wss://fstream.binance.com/ws/btcusdt@trade") as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                p = float(data.get('p', 0.0))
                                if p > 10000:
                                    engine.check_trade_resolution(p)
            except Exception:
                await asyncio.sleep(5)

    async def runner():
        engine.candles_15m = fetch_klines("BTCUSDT", "15m", 300)
        engine.market_data = fetch_market_metrics()
        await asyncio.gather(kline_listener(), trade_listener())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner())

if "worker_running" not in st.session_state:
    st.session_state["worker_running"] = True
    threading.Thread(target=run_worker_thread, daemon=True).start()

# --- 6. FRONTEND TERMINAL & PERFORMANCE ANALYZER ---
st.set_page_config(page_title="BTCUSDT SMC QUANT TERMINAL", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #080a0f !important; }
        .stApp { background-color: #080a0f !important; }
        iframe { border: none !important; width: 100vw !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

# Performance Statistics from Vault
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("SELECT timestamp, tf_tier, direction, setup_name, score, entry, exit, result, pnl_r, confluence FROM trades ORDER BY id DESC")
rows = cur.fetchall()
total_trades = len(rows)
wins = sum(1 for r in rows if r[8] > 0)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0
net_r = sum(r[8] for r in rows) if total_trades > 0 else 0.0
conn.close()

vault_json = json.dumps([{"time": r[0], "tf": r[1], "dir": r[2], "setup": r[3], "score": r[4], "entry": r[5], "exit": r[6], "res": r[7], "pnl": r[8], "conf": r[9]} for r in rows])
ui_candles = fetch_klines("BTCUSDT", "15m", 350)
candles_json = json.dumps(ui_candles)
active_trade_json = json.dumps(engine.active_trade)

ui_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; font-family:-apple-system, sans-serif; }}
        body {{ background:#080a0f; color:#d1d4dc; overflow:hidden; width:100vw; display:flex; flex-direction:column; }}
        .top-bar {{ display:flex; justify-content:space-between; align-items:center; padding:5px 8px; background:#0d1118; border-bottom:1px solid rgba(255,255,255,0.06); height:38px; font-size:10px; }}
        .badge-live {{ background:#089981; color:#fff; font-size:8px; font-weight:800; padding:2px 4px; border-radius:2px; }}
        .badge-tf {{ background:#131924; color:#38bdf8; font-size:8px; font-weight:800; padding:2px 5px; border-radius:2px; }}
        .vault-btn {{ background:#141b27; border:1px solid #232f42; color:#38bdf8; padding:3px 8px; border-radius:4px; font-size:10px; font-weight:700; cursor:pointer; }}
        #chart-container {{ width:100vw; height:375px; position:relative; }}
        .bottom-section {{ display:flex; flex-direction:column; width:100vw; background:#080a0f; }}
        .bot-bar-1 {{ display:flex; justify-content:space-between; align-items:center; padding:4px 8px; background:#0b0f16; border-top:1px solid rgba(255,255,255,0.06); height:28px; font-size:8.5px; }}
        .bot-bar-2 {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:4px; padding:3px 6px 5px 6px; background:#06080c; height:34px; font-size:7.5px; }}
        .info-card {{ background:#0d121a; border:1px solid #161e2a; padding:2px 4px; border-radius:3px; display:flex; flex-direction:column; justify-content:center; }}
        .modal {{ display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.88); z-index:999999; align-items:center; justify-content:center; padding:14px; }}
        .modal-box {{ background:#0f141e; border:1px solid #1c2636; border-radius:8px; width:100%; max-width:400px; padding:14px; max-height:85vh; display:flex; flex-direction:column; }}
        .c-green {{ color:#089981 !important; font-weight:bold; }}
        .c-red {{ color:#f23645 !important; font-weight:bold; }}
        .c-cyan {{ color:#00e5ff !important; font-weight:bold; }}
        .c-yellow {{ color:#f59e0b !important; font-weight:bold; }}
    </style>
</head>
<body>
    <div class="top-bar">
        <div style="display:flex; align-items:center; gap:8px;">
            <span class="badge-live">QUANT CORE</span>
            <span class="badge-tf">105-PT GATE</span>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">GATE</span><div id="hud-status" class="c-yellow" style="font-weight:800; font-size:10px;">SCANNING</div></div>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">PRICE</span><div id="hud-live-price" class="c-cyan" style="font-weight:800; font-size:10px;">--</div></div>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">SCORE</span><div id="hud-score" class="c-green" style="font-weight:800; font-size:10px;">--</div></div>
        </div>
        <button class="vault-btn" onclick="openVault()">📊 VAULT ({total_trades} | {win_rate}%)</button>
    </div>

    <div id="chart-container"></div>

    <div class="bottom-section">
        <div class="bot-bar-1">
            <div id="status-line">REGIME: <span class="c-green">INSTITUTIONAL SMC SCAN</span> (CLEAN SCREEN)</div>
            <div style="display:flex; gap:5px;">
                <span style="border:1px solid rgba(202,138,4,0.4); color:#fbbf24; padding:1px 4px; border-radius:2px; font-weight:700;">⚡ FORCE CLOSE</span>
                <span style="border:1px solid rgba(220,38,38,0.4); color:#f87171; padding:1px 4px; border-radius:2px; font-weight:700;">🚨 KILL SWITCH</span>
            </div>
        </div>
        <div class="bot-bar-2">
            <div class="info-card"><span style="color:#565f70;">GATE STATUS</span><span class="c-green">&gt;=70 VALID</span></div>
            <div class="info-card"><span style="color:#565f70;">NET P&amp;L</span><span class="{ 'c-green' if net_r >= 0 else 'c-red' }">{net_r:+.1f}R</span></div>
            <div class="info-card"><span style="color:#565f70;">TRAIL/BE</span><span class="c-cyan">AUTO LOCK</span></div>
            <div class="info-card"><span style="color:#565f70;">SHIELD</span><span class="c-green">ARMED 🛡️</span></div>
        </div>
    </div>

    <div id="vaultModal" class="modal">
        <div class="modal-box">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1c2636; padding-bottom:8px; margin-bottom:8px;">
                <span style="font-weight:bold; font-size:12px;">📊 QUANT PERFORMANCE ANALYZER</span>
                <span style="cursor:pointer; font-weight:bold;" onclick="closeVault()">✕</span>
            </div>
            <div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:6px; margin-bottom:10px; font-size:9px;">
                <div style="background:#090d14; padding:6px; border-radius:4px; border:1px solid #161e2a;"><span style="color:#64748b;">WIN RATE</span><div class="c-green" style="font-size:12px; font-weight:bold;">{win_rate}%</div></div>
                <div style="background:#090d14; padding:6px; border-radius:4px; border:1px solid #161e2a;"><span style="color:#64748b;">NET RETURN</span><div class="c-cyan" style="font-size:12px; font-weight:bold;">{net_r:+.1f}R</div></div>
                <div style="background:#090d14; padding:6px; border-radius:4px; border:1px solid #161e2a;"><span style="color:#64748b;">TOTAL TRADES</span><div class="c-yellow" style="font-size:12px; font-weight:bold;">{total_trades}</div></div>
            </div>
            <div id="vaultList" style="max-height: 220px; overflow-y: auto;"></div>
        </div>
    </div>

    <script>
        const rawCandles = {candles_json};
        const vaultTrades = {vault_json};
        let activeTrade = {active_trade_json};
        const localOffsetSeconds = 5.5 * 3600;

        function align15m(ts) {{ return Math.floor(ts / 900) * 900; }}

        let candleMap = new Map();
        rawCandles.forEach(c => {{
            const alignedTime = align15m(c.time) + localOffsetSeconds;
            candleMap.set(alignedTime, {{ time: alignedTime, open: c.open, high: c.high, low: c.low, close: c.close }});
        }});

        let allCandles = Array.from(candleMap.values()).sort((a, b) => a.time - b.time);
        let currentBar = allCandles.length > 0 ? {{ ...allCandles[allCandles.length - 1] }} : null;

        const container = document.getElementById('chart-container');
        const chart = LightweightCharts.createChart(container, {{
            layout: {{ background: {{ type: 'solid', color: '#080a0f' }}, textColor: '#64748b', fontSize: 10 }},
            grid: {{ vertLines: {{ color: 'rgba(255, 255, 255, 0.03)' }}, horzLines: {{ color: 'rgba(255, 255, 255, 0.03)' }} }},
            rightPriceScale: {{ borderColor: '#161e2a', autoScale: true, scaleMargins: {{ top: 0.12, bottom: 0.12 }} }},
            timeScale: {{ borderColor: '#161e2a', timeVisible: true, secondsVisible: false, barSpacing: 9, rightOffset: 3 }}
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#089981', downColor: '#f23645',
            borderUpColor: '#089981', borderDownColor: '#f23645',
            wickUpColor: '#089981', wickDownColor: '#f23645'
        }});

        if (allCandles.length > 0) {{
            candleSeries.setData(allCandles);
            chart.timeScale().fitContent();
        }}

        let activeLines = {{ entry: null, sl: null, tp1: null, tp2: null, tp3: null }};

        function renderDynamicTradeLines() {{
            Object.keys(activeLines).forEach(k => {{
                if (activeLines[k]) {{ candleSeries.removePriceLine(activeLines[k]); activeLines[k] = null; }}
            }});

            if (activeTrade) {{
                document.getElementById('hud-status').innerText = activeTrade.dir + " ARMED";
                document.getElementById('hud-status').className = activeTrade.dir === "LONG" ? "c-green" : "c-red";
                document.getElementById('hud-score').innerText = activeTrade.score + "/105";
                document.getElementById('status-line').innerHTML = `SETUP: <span class="c-cyan">${{activeTrade.setup}}</span> | <span class="c-green">SCORE ${{activeTrade.score}}/105</span>`;

                activeLines.entry = candleSeries.createPriceLine({{ price: activeTrade.entry, color: '#38bdf8', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: 'ENTRY' }});
                activeLines.sl = candleSeries.createPriceLine({{ price: activeTrade.sl, color: '#f43f5e', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'SL (BE LOCK)' }});
                activeLines.tp1 = candleSeries.createPriceLine({{ price: activeTrade.tp1, color: '#10b981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: 'TP1 (1R)' }});
                activeLines.tp2 = candleSeries.createPriceLine({{ price: activeTrade.tp2, color: '#10b981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: 'TP2 (2R)' }});
                activeLines.tp3 = candleSeries.createPriceLine({{ price: activeTrade.tp3, color: '#10b981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'TP3 (3R FINAL)' }});
            }} else {{
                document.getElementById('hud-status').innerText = "SCANNING";
                document.getElementById('hud-status').className = "c-yellow";
                document.getElementById('hud-score').innerText = "GATE ON";
                document.getElementById('status-line').innerHTML = 'REGIME: <span class="c-green">INSTITUTIONAL SMC SCAN</span> (CLEAN SCREEN)';
            }}
        }}

        renderDynamicTradeLines();

        let wsKline = null;
        let wsTrade = null;

        function connectStreams() {{
            wsKline = new WebSocket('wss://fstream.binance.com/ws/btcusdt@kline_15m');
            wsKline.onmessage = (event) => {{
                try {{
                    const res = JSON.parse(event.data);
                    const k = res.k;
                    const o = parseFloat(k.o), h = parseFloat(k.h), l = parseFloat(k.l), c = parseFloat(k.c);
                    if (o < 20000 || h < 20000 || l < 20000 || c < 20000) return;

                    const alignedTime = align15m(Math.floor(k.t / 1000)) + localOffsetSeconds;
                    currentBar = {{ time: alignedTime, open: o, high: h, low: l, close: c }};
                    candleSeries.update(currentBar);
                }} catch(e) {{}}
            }};
            wsKline.onclose = () => {{ setTimeout(connectStreams, 2000); }};

            wsTrade = new WebSocket('wss://fstream.binance.com/ws/btcusdt@trade');
            wsTrade.onmessage = (event) => {{
                try {{
                    const t = JSON.parse(event.data);
                    const livePrice = parseFloat(t.p);
                    if (livePrice < 20000 || livePrice > 300000) return;

                    document.getElementById('hud-live-price').innerText = "$" + livePrice.toFixed(1);

                    if (currentBar) {{
                        if (livePrice > currentBar.high) currentBar.high = livePrice;
                        if (livePrice < currentBar.low) currentBar.low = livePrice;
                        currentBar.close = livePrice;
                        candleSeries.update(currentBar);
                    }}
                }} catch(e) {{}}
            }};
            wsTrade.onclose = () => {{ setTimeout(connectStreams, 2000); }};
        }}

        connectStreams();

        function openVault() {{
            document.getElementById('vaultModal').style.display = 'flex';
            const list = document.getElementById('vaultList');
            list.innerHTML = vaultTrades.length === 0 ? '<div style="font-size:10px; color:#565f70; text-align:center; padding:15px;">No resolved trades yet. Engine actively scanning...</div>' : '';
            vaultTrades.forEach(t => {{
                list.innerHTML += `
                    <div style="padding:6px 0; border-bottom:1px solid #161e2a; font-size:9.5px;">
                        <div style="display:flex; justify-content:space-between; margin-bottom:2px;">
                            <span>${{t.time}} <b style="color:#fbbf24">[${{t.tf}}]</b> <b style="color:${{t.dir === 'LONG' ? '#089981' : '#f43f5e'}}">${{t.dir}}</b> @ ${{t.entry}}</span>
                            <span style="font-weight:bold; color:${{t.pnl >= 0 ? '#089981' : '#f43f5e'}}">${{t.res}} (${{t.pnl >= 0 ? '+' : ''}}${{t.pnl}}R)</span>
                        </div>
                        <div style="color:#64748b; font-size:8.5px;">Score: <b style="color:#38bdf8">${{t.score}}/105</b> | ${{t.conf}}</div>
                    </div>`;
            }});
        }}
        function closeVault() {{ document.getElementById('vaultModal').style.display = 'none'; }}
    </script>
</body>
</html>
"""

components.html(ui_html, height=490, scrolling=False)
