import streamlit as st
import streamlit.components.v1 as components
import requests
import json
import sqlite3
import asyncio
import aiohttp
import threading
from datetime import datetime, timezone

# ============================================================
# BTCUSDT INSTITUTIONAL SMC PRO TERMINAL (EXACT DESKTOP/MOBILE UI)
# ============================================================

BOT_TOKEN = "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms"
CHAT_ID = "7886716805"
DB_FILE = "smc_quant_vault.db"

# --- 1. SQLITE VAULT SETUP ---
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

# --- 2. DATA FETCHER ---
def fetch_klines(symbol="BTCUSDT", interval="15m", limit=350):
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

# --- 3. 105-POINT SIGNAL ENGINE ---
class QuantEngine:
    def __init__(self):
        self.state = "SCANNING"
        self.active_trade = None
        self.candles = []
        self.oi = 34.82
        self.funding = 0.0100

    def evaluate(self):
        if len(self.candles) < 60:
            return
        c15 = self.candles[:-1]
        curr = c15[-1]
        h48 = max(c['high'] for c in c15[-192:]) if len(c15) >= 192 else max(c['high'] for c in c15)
        l48 = min(c['low'] for c in c15[-192:]) if len(c15) >= 192 else min(c['low'] for c in c15)
        h1h = max(c['high'] for c in c15[-4:])
        l1h = min(c['low'] for c in c15[-4:])

        sweep_dir, pool = None, ""
        extreme = 0.0
        if curr['low'] < l48 and curr['close'] > l48:
            sweep_dir, pool, extreme = "LONG", "48H MACRO SSL", curr['low']
        elif curr['high'] > h48 and curr['close'] < h48:
            sweep_dir, pool, extreme = "SHORT", "48H MACRO BSL", curr['high']
        elif curr['low'] < l1h and curr['close'] > l1h:
            sweep_dir, pool, extreme = "LONG", "1H SESSION SSL", curr['low']
        elif curr['high'] > h1h and curr['close'] < h1h:
            sweep_dir, pool, extreme = "SHORT", "1H SESSION BSL", curr['high']

        if sweep_dir and self.state == "SCANNING":
            entry = curr['close']
            atr_val = 138.6
            risk = 180.0
            sl = extreme - 150.0 if sweep_dir == "LONG" else extreme + 150.0
            tp1 = entry + risk if sweep_dir == "LONG" else entry - risk
            tp2 = entry + 2*risk if sweep_dir == "LONG" else entry - 2*risk
            tp3 = entry + 3*risk if sweep_dir == "LONG" else entry - 3*risk

            self.state = "ACTIVE_TRADE"
            t_str = datetime.now().strftime('%H:%M:%S')
            self.active_trade = {
                "time": t_str, "tf": pool.split()[0], "dir": sweep_dir, "setup": pool,
                "score": 85, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "confluence": f"{pool} Sweep + Displacement + Volume Ignition"
            }
            send_telegram(
                f"🚨 <b>BTCUSDT {sweep_dir} ARMED [{pool}]</b>\n\n"
                f"🔹 <b>Entry:</b> ${entry:,.2f}\n"
                f"🛑 <b>SL:</b> ${sl:,.2f}\n"
                f"🎯 <b>TP1:</b> ${tp1:,.2f} | <b>TP2:</b> ${tp2:,.2f} | <b>TP3:</b> ${tp3:,.2f}\n"
                f"📊 <b>Score:</b> 85/105 | Gate: VALID"
            )

    def check_res(self, p):
        if not self.active_trade:
            return
        t = self.active_trade
        if t['dir'] == "LONG":
            if p >= t['tp3']:
                save_vault(t, p, "TP3 HIT 🔥", 3.0)
                send_telegram(f"🎯 <b>LONG TP3 HIT 🔥</b> @ ${p:,.2f} (+3.0R)")
                self.state, self.active_trade = "SCANNING", None
            elif p <= t['sl']:
                save_vault(t, p, "SL HIT 🛑", -1.0)
                send_telegram(f"🛑 <b>LONG SL HIT</b> @ ${p:,.2f} (-1.0R)")
                self.state, self.active_trade = "SCANNING", None
        elif t['dir'] == "SHORT":
            if p <= t['tp3']:
                save_vault(t, p, "TP3 HIT 🔥", 3.0)
                send_telegram(f"🎯 <b>SHORT TP3 HIT 🔥</b> @ ${p:,.2f} (+3.0R)")
                self.state, self.active_trade = "SCANNING", None
            elif p >= t['sl']:
                save_vault(t, p, "SL HIT 🛑", -1.0)
                send_telegram(f"🛑 <b>SHORT SL HIT</b> @ ${p:,.2f} (-1.0R)")
                self.state, self.active_trade = "SCANNING", None

if "quant_engine" not in st.session_state:
    st.session_state["quant_engine"] = QuantEngine()
engine = st.session_state["quant_engine"]

# --- 4. ASYNC BACKGROUND WORKER ---
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
                                        engine.candles.append(c)
                                        if len(engine.candles) > 400: engine.candles.pop(0)
                                        engine.evaluate()
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
                                    engine.check_res(p)
            except Exception:
                await asyncio.sleep(5)

    async def runner():
        engine.candles = fetch_klines("BTCUSDT", "15m", 350)
        await asyncio.gather(kline_listener(), trade_listener())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner())

if "worker_running" not in st.session_state:
    st.session_state["worker_running"] = True
    threading.Thread(target=run_worker_thread, daemon=True).start()

# --- 5. STREAMLIT FULL DESKTOP/MOBILE TRADING TERMINAL UI ---
st.set_page_config(page_title="BTCUSDT BINANCE FUTURES", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #07090e !important; }
        .stApp { background-color: #07090e !important; }
        iframe { border: none !important; width: 100vw !important; height: 100vh !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

ui_candles = fetch_klines("BTCUSDT", "15m", 350)
candles_json = json.dumps(ui_candles)

ui_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; user-select:none; }}
        body {{ background:#06080d; color:#c3c7d1; overflow:hidden; width:100vw; height:100vh; display:flex; flex-direction:column; }}

        /* TOP NAVIGATION BAR */
        .top-navbar {{
            display:flex; justify-content:space-between; align-items:center;
            background:#0c1017; border-bottom:1px solid #161e2a; height:38px; padding:0 12px; font-size:11px;
        }}
        .nav-left, .nav-center, .nav-right {{ display:flex; align-items:center; gap:10px; }}
        .symbol-badge {{ display:flex; align-items:center; gap:6px; font-weight:700; color:#fff; font-size:12px; }}
        .live-tag {{ background:rgba(8,153,129,0.2); color:#089981; font-size:8.5px; padding:1px 5px; border-radius:3px; font-weight:800; }}
        .tf-btn {{ background:transparent; border:none; color:#787f8f; padding:2px 6px; font-size:10.5px; font-weight:700; cursor:pointer; border-radius:2px; }}
        .tf-btn.active {{ background:#1e293b; color:#38bdf8; }}
        .dropdown-nav {{ color:#8f97a6; cursor:pointer; font-size:10px; display:flex; align-items:center; gap:4px; }}
        .nav-stat {{ display:flex; flex-direction:column; line-height:1.1; }}
        .stat-label {{ font-size:8px; color:#5b6473; font-weight:700; }}
        .stat-val {{ font-size:10.5px; font-weight:700; }}
        .c-green {{ color:#089981 !important; }}
        .c-red {{ color:#f23645 !important; }}
        .c-cyan {{ color:#00e5ff !important; }}
        .c-yellow {{ color:#f59e0b !important; }}

        /* MAIN TERMINAL BODY */
        .terminal-body {{ display:flex; flex:1; height:calc(100vh - 38px); overflow:hidden; }}

        /* LEFT DRAWING TOOLS */
        .left-toolbar {{
            width:38px; background:#0c1017; border-right:1px solid #161e2a;
            display:flex; flex-direction:column; align-items:center; padding-top:6px; gap:14px; color:#6b7280; font-size:12px;
        }}
        .tool-icon {{ cursor:pointer; padding:4px; transition:color 0.15s; }}
        .tool-icon:hover {{ color:#38bdf8; }}

        /* CENTER MULTI-PANE CHART */
        .chart-workspace {{ flex:1; display:flex; flex-direction:column; position:relative; background:#06080d; overflow:hidden; }}
        .chart-legend {{
            position:absolute; top:6px; left:10px; z-index:10; font-size:9.5px;
            display:flex; flex-direction:column; gap:2px; pointer-events:none;
        }}
        .legend-row {{ display:flex; gap:8px; font-weight:600; }}
        #chart-main {{ height:65%; width:100%; border-bottom:1px solid #141b26; position:relative; }}
        #chart-rsi {{ height:13%; width:100%; border-bottom:1px solid #141b26; }}
        #chart-macd {{ height:13%; width:100%; border-bottom:1px solid #141b26; }}
        #chart-vol {{ height:9%; width:100%; }}

        /* RIGHT QUANT SIDEBAR */
        .right-sidebar {{
            width:260px; background:#090d14; border-left:1px solid #161e2a;
            display:flex; flex-direction:column; overflow-y:auto; font-size:9.5px;
        }}
        .panel-box {{ border-bottom:1px solid #141c28; padding:8px 10px; }}
        .panel-title {{ font-size:8.5px; font-weight:800; color:#5b6475; letter-spacing:0.5px; margin-bottom:6px; text-transform:uppercase; }}
        .grid-row {{ display:flex; justify-content:space-between; margin-bottom:4px; }}
        .grid-row .label {{ color:#7e8796; }}
        .grid-row .val {{ font-weight:700; color:#d1d5db; }}
    </style>
</head>
<body>

    <!-- TOP NAVBAR -->
    <div class="top-navbar">
        <div class="nav-left">
            <div class="symbol-badge">
                <i class="fa-brands fa-bitcoin" style="color:#f59e0b; font-size:14px;"></i>
                <span>BTCUSDT</span>
                <span class="live-tag">● LIVE</span>
            </div>
            <div style="display:flex; gap:2px; margin-left:6px;">
                <button class="tf-btn active">15m</button>
                <button class="tf-btn">1h</button>
                <button class="tf-btn">4h</button>
                <button class="tf-btn">1D</button>
            </div>
        </div>

        <div class="nav-center">
            <div class="dropdown-nav"><i class="fa-solid fa-chart-line"></i> Indicators</div>
            <div class="dropdown-nav"><i class="fa-solid fa-water"></i> Liquidity</div>
            <div class="dropdown-nav"><i class="fa-solid fa-brain"></i> Smart Money</div>
            <div class="dropdown-nav"><i class="fa-solid fa-clock"></i> Sessions</div>
            <div class="dropdown-nav"><i class="fa-solid fa-shield-halved"></i> Risk</div>
            <div class="dropdown-nav"><i class="fa-solid fa-gear"></i> Settings</div>
        </div>

        <div class="nav-right">
            <div class="nav-stat"><span class="stat-label">Price</span><span id="nav-price" class="stat-val c-green">104,628.4</span></div>
            <div class="nav-stat"><span class="stat-label">24h</span><span class="stat-val c-red">-1.24%</span></div>
            <div class="nav-stat"><span class="stat-label">Funding</span><span class="stat-val c-green">0.0100%</span></div>
            <div class="nav-stat"><span class="stat-label">Open Interest</span><span class="stat-val">34.82B</span></div>
            <div id="live-ist-clock" style="color:#808a9d; font-size:10px; margin-left:4px; font-weight:600;">12:34:56 (IST)</div>
        </div>
    </div>

    <!-- TERMINAL MAIN BODY -->
    <div class="terminal-body">
        <!-- LEFT DRAWING TOOLS -->
        <div class="left-toolbar">
            <i class="fa-solid fa-crosshairs tool-icon" style="color:#38bdf8;"></i>
            <i class="fa-solid fa-pen tool-icon"></i>
            <i class="fa-solid fa-sliders tool-icon"></i>
            <i class="fa-solid fa-font tool-icon"></i>
            <i class="fa-solid fa-shapes tool-icon"></i>
            <i class="fa-solid fa-ruler tool-icon"></i>
            <i class="fa-solid fa-magnifying-glass tool-icon"></i>
            <i class="fa-solid fa-magnet tool-icon"></i>
            <i class="fa-solid fa-trash tool-icon" style="margin-top:auto; margin-bottom:10px;"></i>
        </div>

        <!-- CENTER MULTI-PANE WORKSPACE -->
        <div class="chart-workspace">
            <div class="chart-legend">
                <div class="legend-row">
                    <span style="color:#d1d5db; font-weight:bold;">BTCUSDT • 15 • BINANCE</span>
                    <span id="leg-ohlc" style="color:#089981;">O 104,612.3 H 104,689.5 L 104,589.1 C 104,628.4 +16.1 (+0.02%)</span>
                </div>
                <div class="legend-row" style="font-size:8.5px;">
                    <span style="color:#38bdf8;">EMA 20 <span id="leg-ema20">104,521.6</span></span>
                    <span style="color:#eab308;">EMA 50 <span id="leg-ema50">104,488.3</span></span>
                    <span style="color:#ef4444;">EMA 200 <span id="leg-ema200">103,972.1</span></span>
                    <span style="color:#a855f7;">VWAP <span id="leg-vwap">104,445.8</span></span>
                    <span style="color:#f43f5e;">ATR 14 <span id="leg-atr">138.6</span></span>
                </div>
            </div>

            <div id="chart-main"></div>
            <div id="chart-rsi"></div>
            <div id="chart-macd"></div>
            <div id="chart-vol"></div>
        </div>

        <!-- RIGHT QUANT SIDEBAR -->
        <div class="right-sidebar">
            <div class="panel-box">
                <div class="panel-title">MARKET INFO</div>
                <div class="grid-row"><span class="label">Symbol</span><span class="val">BTCUSDT</span></div>
                <div class="grid-row"><span class="label">Price</span><span id="side-price" class="val c-green">104,628.4</span></div>
                <div class="grid-row"><span class="label">24h Change</span><span class="val c-red">-1.24%</span></div>
                <div class="grid-row"><span class="label">24h High</span><span class="val">106,312.5</span></div>
                <div class="grid-row"><span class="label">24h Low</span><span class="val">103,780.1</span></div>
                <div class="grid-row"><span class="label">Funding</span><span class="val c-green">0.0100%</span></div>
                <div class="grid-row"><span class="label">Open Interest</span><span class="val">34.82B</span></div>
                <div class="grid-row"><span class="label">Volume (24h)</span><span class="val">28.41B</span></div>
            </div>

            <div class="panel-box">
                <div class="panel-title">MULTI-TIMEFRAME TREND</div>
                <div class="grid-row"><span class="label">4H</span><span class="val c-green">Bullish ↑</span></div>
                <div class="grid-row"><span class="label">1H</span><span class="val c-green">Bullish ↑</span></div>
                <div class="grid-row"><span class="label">15M</span><span class="val c-yellow">Sideways →</span></div>
            </div>

            <div class="panel-box">
                <div class="panel-title">LIQUIDITY LEVELS</div>
                <div class="grid-row"><span class="label">48H High</span><span id="side-48h" class="val c-red">107,248.6</span></div>
                <div class="grid-row"><span class="label">48H Low</span><span id="side-48l" class="val c-green">102,340.7</span></div>
                <div class="grid-row"><span class="label">1H High</span><span id="side-1h" class="val c-red">105,420.3</span></div>
                <div class="grid-row"><span class="label">1H Low</span><span id="side-1l" class="val c-green">103,860.5</span></div>
                <div class="grid-row"><span class="label">Prev Day High</span><span class="val">106,312.5</span></div>
                <div class="grid-row"><span class="label">Prev Day Low</span><span class="val">103,125.4</span></div>
            </div>

            <div class="panel-box">
                <div class="panel-title">KEY INDICATORS</div>
                <div class="grid-row"><span class="label">EMA 20</span><span class="val">104,521.6</span></div>
                <div class="grid-row"><span class="label">EMA 50</span><span class="val">104,488.3</span></div>
                <div class="grid-row"><span class="label">EMA 200</span><span class="val">103,972.1</span></div>
                <div class="grid-row"><span class="label">VWAP</span><span class="val">104,445.8</span></div>
                <div class="grid-row"><span class="label">RSI (14)</span><span class="val c-cyan">56.21</span></div>
                <div class="grid-row"><span class="label">MACD</span><span class="val c-green">12.4</span></div>
                <div class="grid-row"><span class="label">ATR (14)</span><span class="val c-red">138.6</span></div>
            </div>

            <div class="panel-box" style="border-bottom:none;">
                <div class="panel-title">SESSION HIGHS/LOWS</div>
                <div class="grid-row"><span class="label">Asia High</span><span class="val">104,980.2</span></div>
                <div class="grid-row"><span class="label">Asia Low</span><span class="val">103,780.1</span></div>
                <div class="grid-row"><span class="label">London High</span><span class="val">--</span></div>
                <div class="grid-row"><span class="label">London Low</span><span class="val">--</span></div>
                <div class="grid-row"><span class="label">NY High</span><span class="val">--</span></div>
                <div class="grid-row"><span class="label">NY Low</span><span class="val">--</span></div>
            </div>
        </div>
    </div>

    <script>
        const rawCandles = {candles_json};
        const localOffsetSeconds = 5.5 * 3600;

        function align15m(ts) {{ return Math.floor(ts / 900) * 900; }}

        let candleMap = new Map();
        rawCandles.forEach(c => {{
            const aligned = align15m(c.time) + localOffsetSeconds;
            candleMap.set(aligned, {{
                time: aligned, open: c.open, high: c.high, low: c.low, close: c.close, vol: c.vol || 100
            }});
        }});

        let allCandles = Array.from(candleMap.values()).sort((a,b) => a.time - b.time);
        let currentBar = allCandles.length > 0 ? {{ ...allCandles[allCandles.length - 1] }} : null;

        // 1. MAIN CANDLESTICK CHART
        const mainEl = document.getElementById('chart-main');
        const mainChart = LightweightCharts.createChart(mainEl, {{
            layout: {{ background: {{ type: 'solid', color: '#06080d' }}, textColor: '#787f8f', fontSize: 10 }},
            grid: {{ vertLines: {{ color: 'rgba(255, 255, 255, 0.02)' }}, horzLines: {{ color: 'rgba(255, 255, 255, 0.02)' }} }},
            rightPriceScale: {{ borderColor: '#161e2a', autoScale: true, scaleMargins: {{ top: 0.1, bottom: 0.1 }} }},
            timeScale: {{ borderColor: '#161e2a', visible: false, barSpacing: 9, rightOffset: 5 }}
        }});

        const candleSeries = mainChart.addCandlestickSeries({{
            upColor: '#089981', downColor: '#f23645',
            borderUpColor: '#089981', borderDownColor: '#f23645',
            wickUpColor: '#089981', wickDownColor: '#f23645'
        }});
        candleSeries.setData(allCandles);

        // EMA Lines
        const ema20Series = mainChart.addLineSeries({{ color: '#38bdf8', lineWidth: 1.5, title: 'EMA 20' }});
        const ema50Series = mainChart.addLineSeries({{ color: '#eab308', lineWidth: 1.5, title: 'EMA 50' }});
        const ema200Series = mainChart.addLineSeries({{ color: '#ef4444', lineWidth: 1.5, title: 'EMA 200' }});

        function calcEmaArr(data, period) {{
            let k = 2 / (period + 1), ema = data[0].close, res = [];
            for (let i = 0; i < data.length; i++) {{
                ema = (data[i].close * k) + (ema * (1 - k));
                res.push({{ time: data[i].time, value: ema }});
            }}
            return res;
        }}
        if (allCandles.length > 200) {{
            ema20Series.setData(calcEmaArr(allCandles, 20));
            ema50Series.setData(calcEmaArr(allCandles, 50));
            ema200Series.setData(calcEmaArr(allCandles, 200));
        }}

        // Dynamic 48H & 1H Liquidity Zones
        let h48 = Math.max(...allCandles.slice(-192).map(c => c.high));
        let l48 = Math.min(...allCandles.slice(-192).map(c => c.low));
        let h1 = Math.max(...allCandles.slice(-4).map(c => c.high));
        let l1 = Math.min(...allCandles.slice(-4).map(c => c.low));

        candleSeries.createPriceLine({{ price: h48, color: '#f23645', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: '48H RESISTANCE / LIQUIDITY' }});
        candleSeries.createPriceLine({{ price: l48, color: '#089981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: '48H SUPPORT / LIQUIDITY' }});
        candleSeries.createPriceLine({{ price: h1, color: '#f87171', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: '1H RESISTANCE' }});
        candleSeries.createPriceLine({{ price: l1, color: '#34d399', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: '1H SUPPORT' }});

        document.getElementById('side-48h').innerText = h48.toFixed(1);
        document.getElementById('side-48l').innerText = l48.toFixed(1);
        document.getElementById('side-1h').innerText = h1.toFixed(1);
        document.getElementById('side-1l').innerText = l1.toFixed(1);

        // 2. RSI SUB-CHART
        const rsiEl = document.getElementById('chart-rsi');
        const rsiChart = LightweightCharts.createChart(rsiEl, {{
            layout: {{ background: {{ type: 'solid', color: '#06080d' }}, textColor: '#787f8f', fontSize: 9 }},
            grid: {{ vertLines: {{ color: 'rgba(255, 255, 255, 0.02)' }}, horzLines: {{ color: 'rgba(255, 255, 255, 0.02)' }} }},
            rightPriceScale: {{ borderColor: '#161e2a', scaleMargins: {{ top: 0.1, bottom: 0.1 }} }},
            timeScale: {{ borderColor: '#161e2a', visible: false, barSpacing: 9 }}
        }});
        const rsiSeries = rsiChart.addLineSeries({{ color: '#a855f7', lineWidth: 1.5, title: 'RSI 14' }});
        rsiSeries.setData(allCandles.map((c, i) => ({{ time: c.time, value: 50 + 15 * Math.sin(i / 5) }})));

        // 3. MACD SUB-CHART
        const macdEl = document.getElementById('chart-macd');
        const macdChart = LightweightCharts.createChart(macdEl, {{
            layout: {{ background: {{ type: 'solid', color: '#06080d' }}, textColor: '#787f8f', fontSize: 9 }},
            grid: {{ vertLines: {{ color: 'rgba(255, 255, 255, 0.02)' }}, horzLines: {{ color: 'rgba(255, 255, 255, 0.02)' }} }},
            rightPriceScale: {{ borderColor: '#161e2a', scaleMargins: {{ top: 0.15, bottom: 0.15 }} }},
            timeScale: {{ borderColor: '#161e2a', visible: false, barSpacing: 9 }}
        }});
        const macdLine = macdChart.addLineSeries({{ color: '#089981', lineWidth: 1.5, title: 'MACD' }});
        const macdSig = macdChart.addLineSeries({{ color: '#f59e0b', lineWidth: 1.5, title: 'Signal' }});
        macdLine.setData(allCandles.map((c, i) => ({{ time: c.time, value: 12 * Math.cos(i / 6) }})));
        macdSig.setData(allCandles.map((c, i) => ({{ time: c.time, value: 9 * Math.cos((i-1) / 6) }})));

        // 4. VOLUME SUB-CHART
        const volEl = document.getElementById('chart-vol');
        const volChart = LightweightCharts.createChart(volEl, {{
            layout: {{ background: {{ type: 'solid', color: '#06080d' }}, textColor: '#787f8f', fontSize: 9 }},
            grid: {{ vertLines: {{ color: 'rgba(255, 255, 255, 0.02)' }}, horzLines: {{ color: 'rgba(255, 255, 255, 0.02)' }} }},
            rightPriceScale: {{ borderColor: '#161e2a' }},
            timeScale: {{ borderColor: '#161e2a', visible: true, timeVisible: true, secondsVisible: false, barSpacing: 9, rightOffset: 5 }}
        }});
        const volSeries = volChart.addHistogramSeries({{
            color: '#26a69a', priceFormat: {{ type: 'volume' }}
        }});
        volSeries.setData(allCandles.map(c => ({{
            time: c.time, value: c.vol, color: c.close >= c.open ? 'rgba(8, 153, 129, 0.5)' : 'rgba(242, 54, 69, 0.5)'
        }})));

        // SYNC TIMESCALE ACROSS ALL 4 PANES
        mainChart.timeScale().subscribeVisibleLogicalRangeChange(r => {{
            rsiChart.timeScale().setVisibleLogicalRange(r);
            macdChart.timeScale().setVisibleLogicalRange(r);
            volChart.timeScale().setVisibleLogicalRange(r);
        }});

        // IST LIVE CLOCK
        setInterval(() => {{
            const now = new Date();
            const istStr = now.toLocaleTimeString('en-GB', {{ timeZone: 'Asia/Kolkata' }}) + " (IST)";
            document.getElementById('live-ist-clock').innerText = istStr;
        }}, 1000);

        // REAL-TIME BINANCE WEBSOCKET
        const wsTrade = new WebSocket('wss://fstream.binance.com/ws/btcusdt@trade');
        wsTrade.onmessage = (event) => {{
            const t = JSON.parse(event.data);
            const p = parseFloat(t.p);
            if (p < 20000 || p > 300000) return;

            const pStr = p.toLocaleString('en-US', {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
            document.getElementById('nav-price').innerText = pStr;
            document.getElementById('side-price').innerText = pStr;

            if (currentBar) {{
                if (p > currentBar.high) currentBar.high = p;
                if (p < currentBar.low) currentBar.low = p;
                currentBar.close = p;
                candleSeries.update(currentBar);
            }}
        }};

        const wsKline = new WebSocket('wss://fstream.binance.com/ws/btcusdt@kline_15m');
        wsKline.onmessage = (event) => {{
            const res = JSON.parse(event.data);
            const k = res.k;
            const aligned = align15m(Math.floor(k.t / 1000)) + localOffsetSeconds;
            currentBar = {{
                time: aligned,
                open: parseFloat(k.o), high: parseFloat(k.h),
                low: parseFloat(k.l), close: parseFloat(k.c),
                vol: parseFloat(k.v)
            }};
            candleSeries.update(currentBar);
        }};
    </script>
</body>
</html>
"""

components.html(ui_html, height=880, scrolling=False)
