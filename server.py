import streamlit as st
import streamlit.components.v1 as components
import requests
import json
import sqlite3
import asyncio
import aiohttp
import threading
from datetime import datetime

BOT_TOKEN = "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms"
CHAT_ID = "7886716805"
DB_FILE = "trades_vault.db"

# --- 1. SQLITE VAULT SETUP ---
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS vault (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            direction TEXT,
            entry REAL,
            sweep_level REAL,
            atr REAL,
            sl REAL,
            tp REAL,
            exit REAL,
            result TEXT,
            pnl TEXT,
            is_win INTEGER
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

def save_vault(trade, exit_price, result, pnl, is_win):
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO vault (timestamp, direction, entry, sweep_level, atr, sl, tp, exit, result, pnl, is_win)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade['time'], trade['dir'], trade['entry'], trade['sweep_level'],
            trade['atr'], trade['sl'], trade['tp'], exit_price, result, pnl, is_win
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass

# --- 2. MULTI-SOURCE ROBUST HISTORICAL BUFFER ---
def get_historical_candles(limit=580):
    endpoints = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit={limit}"
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.5).json()
            if isinstance(r, list) and len(r) > 100:
                candles = []
                for b in r:
                    o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
                    if min(o, h, l, c) > 10000:  # Data sanity check
                        candles.append({"time": int(b[0]/1000), "open": o, "high": h, "low": l, "close": c})
                if len(candles) > 100:
                    return candles
        except Exception:
            continue
    return []

# --- 3. BACKGROUND STRATEGY STATE MACHINE ---
class BotEngine:
    def __init__(self):
        self.state = "SCANNING"
        self.sweep_extreme = None
        self.sweep_level = None
        self.sweep_candle_time = None
        self.active_trade = None
        self.candles_data = []

    def calculate_atr(self, data, period=14):
        if len(data) < period + 1:
            return 85.0
        trs = []
        for i in range(len(data) - period, len(data)):
            hl = data[i]['high'] - data[i]['low']
            hc = abs(data[i]['high'] - data[i - 1]['close'])
            lc = abs(data[i]['low'] - data[i - 1]['close'])
            trs.append(max(hl, hc, lc))
        return sum(trs) / period

    def get_48h_bounds(self, data):
        slice_576 = data[-576:] if len(data) >= 576 else data
        h48 = max(c['high'] for c in slice_576)
        l48 = min(c['low'] for c in slice_576)
        return h48, l48

    def on_closed_candle(self, closed_candle):
        self.candles_data.append(closed_candle)
        if len(self.candles_data) > 650:
            self.candles_data.pop(0)

        completed = self.candles_data[:-1]
        if len(completed) < 50:
            return
        h48, l48 = self.get_48h_bounds(completed)
        atr = self.calculate_atr(completed, 14)

        if self.state == "SCANNING":
            if closed_candle['low'] < l48:
                self.state = "SWEEP_LOW_WAIT"
                self.sweep_extreme = closed_candle['low']
                self.sweep_level = l48
                self.sweep_candle_time = closed_candle['time']
            elif closed_candle['high'] > h48:
                self.state = "SWEEP_HIGH_WAIT"
                self.sweep_extreme = closed_candle['high']
                self.sweep_level = h48
                self.sweep_candle_time = closed_candle['time']

        elif self.state == "SWEEP_LOW_WAIT":
            if closed_candle['time'] > self.sweep_candle_time:
                if closed_candle['close'] > self.sweep_level:
                    entry = closed_candle['close']
                    sl = self.sweep_extreme - (1.6 * atr)
                    risk = entry - sl
                    tp = entry + (2.5 * risk)
                    self.arm_trade("LONG", entry, sl, tp, self.sweep_level, atr)
                else:
                    if closed_candle['low'] < self.sweep_extreme:
                        self.sweep_extreme = closed_candle['low']
                    else:
                        self.state = "SCANNING"

        elif self.state == "SWEEP_HIGH_WAIT":
            if closed_candle['time'] > self.sweep_candle_time:
                if closed_candle['close'] < self.sweep_level:
                    entry = closed_candle['close']
                    sl = self.sweep_extreme + (1.6 * atr)
                    risk = sl - entry
                    tp = entry - (2.5 * risk)
                    self.arm_trade("SHORT", entry, sl, tp, self.sweep_level, atr)
                else:
                    if closed_candle['high'] > self.sweep_extreme:
                        self.sweep_extreme = closed_candle['high']
                    else:
                        self.state = "SCANNING"

    def arm_trade(self, dir_type, entry, sl, tp, s_level, atr_val):
        self.state = "ACTIVE_TRADE"
        t_str = datetime.now().strftime('%H:%M:%S')
        self.active_trade = {
            "dir": dir_type, "entry": entry, "sl": sl, "tp": tp,
            "sweep_level": s_level, "atr": atr_val, "time": t_str
        }
        msg = (
            f"🚨 <b>BTCUSDT {dir_type} PRE-SIGNAL ARMED</b> 🚨\n\n"
            f"⏱ <b>Timeframe:</b> 5M (48H Liquidity Sweep)\n"
            f"🎯 <b>Status:</b> Next-Candle Confirmed\n\n"
            f"🔹 <b>Entry:</b> ${entry:.2f}\n"
            f"🛑 <b>Stop Loss:</b> ${sl:.2f}\n"
            f"🎯 <b>Take Profit (2.5R):</b> ${tp:.2f}\n"
            f"⚡ <b>RR Ratio:</b> 1:2.5"
        )
        send_telegram(msg)

    def resolve_trade(self, result, pnl, is_win, exit_price):
        if not self.active_trade:
            return
        save_vault(self.active_trade, exit_price, result, pnl, is_win)
        msg = (
            f"🏁 <b>TRADE RESOLVED: {result}</b>\n\n"
            f"📌 <b>Direction:</b> {self.active_trade['dir']}\n"
            f"🔹 <b>Entry:</b> ${self.active_trade['entry']:.2f}\n"
            f"🔸 <b>Exit Price:</b> ${exit_price:.2f}\n"
            f"💰 <b>Result:</b> {pnl}\n\n"
            f"🔄 <i>State Reset: SCANNING...</i>"
        )
        send_telegram(msg)
        self.state = "SCANNING"
        self.active_trade = None

# Global Engine
if "bg_engine" not in st.session_state:
    st.session_state["bg_engine"] = BotEngine()
engine = st.session_state["bg_engine"]

# --- 4. SAFE 24/7 BACKGROUND WORKER ---
def run_worker_thread():
    async def kline_listener():
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect("wss://fstream.binance.com/ws/btcusdt@kline_5m") as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                k = data.get('k', {})
                                if k.get('x'):
                                    c = {
                                        "time": int(k['t'] / 1000),
                                        "open": float(k['o']), "high": float(k['h']),
                                        "low": float(k['l']), "close": float(k['c'])
                                    }
                                    if min(c['open'], c['high'], c['low'], c['close']) > 10000:
                                        engine.on_closed_candle(c)
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
                                if p > 10000 and engine.state == "ACTIVE_TRADE" and engine.active_trade:
                                    t = engine.active_trade
                                    if t['dir'] == "LONG":
                                        if p >= t['tp']: engine.resolve_trade("TP HIT 🔥", "+2.5R", 1, p)
                                        elif p <= t['sl']: engine.resolve_trade("SL HIT 🛑", "-1R", 0, p)
                                    elif t['dir'] == "SHORT":
                                        if p <= t['tp']: engine.resolve_trade("TP HIT 🔥", "+2.5R", 1, p)
                                        elif p >= t['sl']: engine.resolve_trade("SL HIT 🛑", "-1R", 0, p)
            except Exception:
                await asyncio.sleep(5)

    async def runner():
        engine.candles_data = get_historical_candles(580)
        await asyncio.gather(kline_listener(), trade_listener())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner())

if "worker_running" not in st.session_state:
    st.session_state["worker_running"] = True
    threading.Thread(target=run_worker_thread, daemon=True).start()

# --- 5. STREAMLIT UI ---
st.set_page_config(page_title="BTCUSDT RADAR 48H", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #080a0f !important; }
        .stApp { background-color: #080a0f !important; }
        iframe { border: none !important; width: 100vw !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

# Fetch Vault Stats
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
rows = cur.fetchall()
total_trades = len(rows)
wins = sum(1 for r in rows if r[5] == 1)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0
conn.close()

vault_json = json.dumps([{"time": r[0], "dir": r[1], "entry": r[2], "res": r[3], "pnl": r[4], "win": r[5]} for r in rows])

ui_candles = get_historical_candles(350)
candles_json = json.dumps(ui_candles)

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
        .modal {{ display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.85); z-index:999999; align-items:center; justify-content:center; padding:16px; }}
        .modal-box {{ background:#0f141e; border:1px solid #1c2636; border-radius:8px; width:100%; max-width:380px; padding:14px; }}
        .c-green {{ color:#089981 !important; font-weight:bold; }}
        .c-red {{ color:#f23645 !important; font-weight:bold; }}
        .c-cyan {{ color:#00e5ff !important; font-weight:bold; }}
        .c-yellow {{ color:#f59e0b !important; font-weight:bold; }}
    </style>
</head>
<body>
    <div class="top-bar">
        <div style="display:flex; align-items:center; gap:8px;">
            <span class="badge-live">LIVE FUTURES</span>
            <span class="badge-tf">5M • 48H</span>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">RADAR</span><div id="hud-status" class="c-yellow" style="font-weight:800; font-size:10px;">SCANNING</div></div>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">PRICE</span><div id="hud-live-price" class="c-cyan" style="font-weight:800; font-size:10px;">--</div></div>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">SL</span><div id="hud-sl" class="c-red" style="font-weight:800; font-size:10px;">--</div></div>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">TP</span><div id="hud-tp" class="c-green" style="font-weight:800; font-size:10px;">--</div></div>
        </div>
        <button class="vault-btn" onclick="openVault()">📜 VAULT ({total_trades})</button>
    </div>

    <div id="chart-container"></div>

    <div class="bottom-section">
        <div class="bot-bar-1">
            <div>ACCOUNT: <span class="c-green">$10.00 BASE</span> | ALLOCATION: <span class="c-cyan">$2.50 (10x)</span></div>
            <div style="display:flex; gap:5px;">
                <span style="border:1px solid rgba(202,138,4,0.4); color:#fbbf24; padding:1px 4px; border-radius:2px; font-weight:700;">⚡ FORCE CLOSE</span>
                <span style="border:1px solid rgba(220,38,38,0.4); color:#f87171; padding:1px 4px; border-radius:2px; font-weight:700;">🚨 KILL SWITCH</span>
            </div>
        </div>
        <div class="bot-bar-2">
            <div class="info-card"><span style="color:#565f70;">SYSTEM</span><span class="c-green">24/7 ONLINE</span></div>
            <div class="info-card"><span style="color:#565f70;">STREAM</span><span id="card-stream" class="c-cyan">FUTURES TICK</span></div>
            <div class="info-card"><span style="color:#565f70;">REGIME</span><span class="c-green">48H SMC</span></div>
            <div class="info-card"><span style="color:#565f70;">SHIELD</span><span class="c-green">ARMED 🛡️</span></div>
        </div>
    </div>

    <div id="vaultModal" class="modal">
        <div class="modal-box">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1c2636; padding-bottom:8px; margin-bottom:8px;">
                <span style="font-weight:bold; font-size:12px;">🔒 SQLITE VAULT</span>
                <span style="cursor:pointer; font-weight:bold;" onclick="closeVault()">✕</span>
            </div>
            <div id="vaultList" style="max-height: 220px; overflow-y: auto;"></div>
        </div>
    </div>

    <script>
        const rawCandles = {candles_json};
        const vaultTrades = {vault_json};

        // Indian Standard Time (IST) Offset: UTC + 5:30 (19800 seconds)
        // Taaki phone me London time (15:30) ki jagah exact local time (21:15) dikhe
        const localOffsetSeconds = 5.5 * 3600;

        const candlesData = rawCandles.map(c => ({{
            time: c.time + localOffsetSeconds,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close
        }}));

        let currentBar = candlesData.length > 0 ? {{ ...candlesData[candlesData.length - 1] }} : null;

        const container = document.getElementById('chart-container');
        const chart = LightweightCharts.createChart(container, {{
            layout: {{ background: {{ type: 'solid', color: '#080a0f' }}, textColor: '#64748b', fontSize: 10 }},
            grid: {{
                vertLines: {{ color: 'rgba(255, 255, 255, 0.03)' }},
                horzLines: {{ color: 'rgba(255, 255, 255, 0.03)' }}
            }},
            rightPriceScale: {{
                borderColor: '#161e2a',
                autoScale: true,
                scaleMargins: {{ top: 0.1, bottom: 0.1 }}
            }},
            timeScale: {{
                borderColor: '#161e2a',
                timeVisible: true,
                secondsVisible: false,
                barSpacing: 8,
                rightOffset: 3
            }}
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#089981', downColor: '#f23645',
            borderUpColor: '#089981', borderDownColor: '#f23645',
            wickUpColor: '#089981', wickDownColor: '#f23645'
        }});

        if (candlesData.length > 0) {{
            candleSeries.setData(candlesData);
            chart.timeScale().fitContent();
        }}

        // --- HIGH INTEGRITY WEBSOCKETS (NO SPIKES, REAL-TIME IST SYNC) ---
        let wsKline = null;
        let wsTrade = null;

        function connectStreams() {{
            // 1. 5M KLINE STREAM
            wsKline = new WebSocket('wss://fstream.binance.com/ws/btcusdt@kline_5m');
            wsKline.onmessage = (event) => {{
                try {{
                    const res = JSON.parse(event.data);
                    const k = res.k;
                    const o = parseFloat(k.o), h = parseFloat(k.h), l = parseFloat(k.l), c = parseFloat(k.c);
                    
                    // Reject corrupted/zero data to avoid vertical line glitch
                    if (o < 20000 || h < 20000 || l < 20000 || c < 20000) return;

                    const candleTime = Math.floor(k.t / 1000) + localOffsetSeconds;
                    currentBar = {{ time: candleTime, open: o, high: h, low: l, close: c }};
                    candleSeries.update(currentBar);
                }} catch(e) {{}}
            }};
            wsKline.onclose = () => {{ setTimeout(connectStreams, 2000); }};

            // 2. LIVE PRICE TICK STREAM (0-BLINK WITH STRICT SPIKE FILTER)
            wsTrade = new WebSocket('wss://fstream.binance.com/ws/btcusdt@trade');
            wsTrade.onmessage = (event) => {{
                try {{
                    const t = JSON.parse(event.data);
                    const livePrice = parseFloat(t.p);

                    // Reject any absurd tick to prevent price crash to 0/-10000
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
            list.innerHTML = vaultTrades.length === 0 ? '<div style="font-size:10px; color:#565f70; text-align:center; padding:15px;">No trades yet. Scanning...</div>' : '';
            vaultTrades.forEach(t => {{
                list.innerHTML += `<div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #161e2a; font-size:10px;">
                    <span>${{t.time}} <b style="color:#00e5ff">${{t.dir}}</b> @ ${{t.entry}}</span>
                    <span style="color:${{t.win === 1 ? '#089981' : '#f23645'}}">${{t.res}} ${{t.pnl}}</span>
                </div>`;
            }});
        }}
        function closeVault() {{ document.getElementById('vaultModal').style.display = 'none'; }}
    </script>
</body>
</html>
"""

components.html(ui_html, height=490, scrolling=False)
