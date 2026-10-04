import streamlit as st
import streamlit.components.v1 as components
import requests
import json
import sqlite3
import asyncio
import aiohttp
import threading
from datetime import datetime

# --- CONFIGURATION & TELEGRAM ---
BOT_TOKEN = "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms"
CHAT_ID = "7886716805"
DB_FILE = "trades_vault_v2.db"  # Fresh clean version to completely fix SQLite error

# --- 1. SQLITE VAULT SETUP (AUTO-MIGRATED) ---
def init_db():
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS vault (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                timeframe TEXT,
                direction TEXT,
                strategy_logic TEXT,
                entry REAL,
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
    except Exception:
        pass

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
            INSERT INTO vault (timestamp, timeframe, direction, strategy_logic, entry, sl, tp, exit, result, pnl, is_win)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade['time'], trade['tf'], trade['dir'], trade['logic'],
            trade['entry'], trade['sl'], trade['tp'], exit_price, result, pnl, is_win
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass

# --- 2. MULTI-SOURCE 15M HISTORICAL CANDLES ---
def get_historical_candles(limit=500):
    endpoints = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=15m&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit={limit}"
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.5).json()
            if isinstance(r, list) and len(r) > 100:
                candles = []
                for b in r:
                    o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
                    if min(o, h, l, c) > 10000:
                        candles.append({"time": int(b[0]/1000), "open": o, "high": h, "low": l, "close": c})
                if len(candles) > 100:
                    return candles
        except Exception:
            continue
    return []

# --- 3. DYNAMIC STRATEGY ENGINE (48H / 1H / 15M) ---
class DynamicExecutionEngine:
    def __init__(self):
        self.state = "SCANNING"
        self.pending_setup = None
        self.active_trade = None
        self.candles_data = []

    def calculate_atr(self, data, period=14):
        if len(data) < period + 1:
            return 120.0
        trs = []
        for i in range(len(data) - period, len(data)):
            hl = data[i]['high'] - data[i]['low']
            hc = abs(data[i]['high'] - data[i - 1]['close'])
            lc = abs(data[i]['low'] - data[i - 1]['close'])
            trs.append(max(hl, hc, lc))
        return sum(trs) / period

    def on_closed_candle(self, closed_candle):
        self.candles_data.append(closed_candle)
        if len(self.candles_data) > 600:
            self.candles_data.pop(0)

        completed = self.candles_data[:-1]
        if len(completed) < 30:
            return
        atr = self.calculate_atr(completed, 14)

        # 48H Levels (192 candles of 15m)
        slice48 = completed[-192:] if len(completed) >= 192 else completed
        h48, l48 = max(c['high'] for c in slice48), min(c['low'] for c in slice48)

        # 1H Levels (4 candles of 15m)
        slice1h = completed[-4:]
        h1h, l1h = max(c['high'] for c in slice1h), min(c['low'] for c in slice1h)

        # 15M Previous Candle Levels
        prev_bar = completed[-1]
        h15m, l15m = prev_bar['high'], prev_bar['low']

        if self.state == "SCANNING":
            # Priority 1: 48H Macro Sweep
            if closed_candle['low'] < l48:
                self.setup_trigger("LONG", "48-HOUR", "48H Macro SSL Swept -> Institutional Bullish Reclaim Confirmation", l48, closed_candle['low'], closed_candle['time'], 3.0)
            elif closed_candle['high'] > h48:
                self.setup_trigger("SHORT", "48-HOUR", "48H Macro BSL Swept -> Institutional Bearish Rejection Confirmation", h48, closed_candle['high'], closed_candle['time'], 3.0)

            # Priority 2: 1H Intermediate Sweep
            elif closed_candle['low'] < l1h:
                self.setup_trigger("LONG", "1-HOUR", "1H Session Low Swept -> Intraday Bullish Reclaim", l1h, closed_candle['low'], closed_candle['time'], 2.0)
            elif closed_candle['high'] > h1h:
                self.setup_trigger("SHORT", "1-HOUR", "1H Session High Swept -> Intraday Bearish Rejection", h1h, closed_candle['high'], closed_candle['time'], 2.0)

            # Priority 3: 15M Local Structure Sweep
            elif closed_candle['low'] < l15m:
                self.setup_trigger("LONG", "15-MINUTE", "15M Prev Low Swept -> Local Structure Bullish CHoCH", l15m, closed_candle['low'], closed_candle['time'], 1.5)
            elif closed_candle['high'] > h15m:
                self.setup_trigger("SHORT", "15-MINUTE", "15M Prev High Swept -> Local Structure Bearish CHoCH", h15m, closed_candle['high'], closed_candle['time'], 1.5)

        elif self.state == "CONFIRMATION_WAIT" and self.pending_setup:
            ps = self.pending_setup
            if closed_candle['time'] > ps['candle_time']:
                if ps['dir'] == "LONG":
                    if closed_candle['close'] > ps['level']:
                        entry = closed_candle['close']
                        sl = ps['extreme'] - (1.2 * atr)
                        risk = entry - sl
                        tp = entry + (ps['rr'] * risk)
                        self.arm_trade("LONG", ps['tf'], ps['logic'], entry, sl, tp, atr, ps['rr'])
                    else:
                        if closed_candle['low'] < ps['extreme']: self.pending_setup['extreme'] = closed_candle['low']
                        else: self.reset_scanner()

                elif ps['dir'] == "SHORT":
                    if closed_candle['close'] < ps['level']:
                        entry = closed_candle['close']
                        sl = ps['extreme'] + (1.2 * atr)
                        risk = sl - entry
                        tp = entry - (ps['rr'] * risk)
                        self.arm_trade("SHORT", ps['tf'], ps['logic'], entry, sl, tp, atr, ps['rr'])
                    else:
                        if closed_candle['high'] > ps['extreme']: self.pending_setup['extreme'] = closed_candle['high']
                        else: self.reset_scanner()

    def setup_trigger(self, dir_type, tf_name, logic_desc, level, extreme, candle_time, rr):
        self.state = "CONFIRMATION_WAIT"
        self.pending_setup = {
            "dir": dir_type, "tf": tf_name, "logic": logic_desc, "level": level,
            "extreme": extreme, "candle_time": candle_time, "rr": rr
        }

    def arm_trade(self, dir_type, tf_name, logic_desc, entry, sl, tp, atr_val, rr):
        self.state = "ACTIVE_TRADE"
        t_str = datetime.now().strftime('%H:%M:%S')
        self.active_trade = {
            "dir": dir_type, "tf": tf_name, "logic": logic_desc, "entry": entry,
            "sl": sl, "tp": tp, "atr": atr_val, "time": t_str, "rr": rr
        }
        emoji = "🟢" if dir_type == "LONG" else "🔴"
        msg = (
            f"{emoji} <b>BTCUSDT {dir_type} EXECUTED [{tf_name} FORMAT]</b>\n\n"
            f"🧠 <b>Strategy Logic:</b> {logic_desc}\n"
            f"🎯 <b>Risk/Reward Ratio:</b> 1:{rr}\n\n"
            f"🔹 <b>Entry:</b> ${entry:,.2f}\n"
            f"🛑 <b>Stop Loss:</b> ${sl:,.2f}\n"
            f"🎯 <b>Take Profit:</b> ${tp:,.2f}\n"
            f"🛡 <b>ATR(14):</b> {atr_val:.1f}\n\n"
            f"<i>Levels active on terminal chart. Monitoring live ticks...</i>"
        )
        send_telegram(msg)
        self.pending_setup = None

    def resolve_trade(self, result, pnl, is_win, exit_price):
        if not self.active_trade:
            return
        save_vault(self.active_trade, exit_price, result, pnl, is_win)
        msg = (
            f"🏁 <b>TRADE RESOLVED: {result}</b>\n\n"
            f"⏱ <b>Format:</b> {self.active_trade['tf']}\n"
            f"📌 <b>Direction:</b> {self.active_trade['dir']}\n"
            f"🔹 <b>Entry:</b> ${self.active_trade['entry']:,.2f}\n"
            f"🔸 <b>Exit Price:</b> ${exit_price:,.2f}\n"
            f"💰 <b>Final P&L:</b> {pnl}\n\n"
            f"🔄 <i>Chart cleared. Scanning next clean institutional liquidity pool...</i>"
        )
        send_telegram(msg)
        self.reset_scanner()

    def reset_scanner(self):
        self.state = "SCANNING"
        self.pending_setup = None
        self.active_trade = None

if "exec_engine" not in st.session_state:
    st.session_state["exec_engine"] = DynamicExecutionEngine()
engine = st.session_state["exec_engine"]

# --- 4. 24/7 BACKGROUND WORKER ---
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
                                        if p >= t['tp']: engine.resolve_trade("TP HIT 🔥", f"+{t['rr']}R", 1, p)
                                        elif p <= t['sl']: engine.resolve_trade("SL HIT 🛑", "-1.0R", 0, p)
                                    elif t['dir'] == "SHORT":
                                        if p <= t['tp']: engine.resolve_trade("TP HIT 🔥", f"+{t['rr']}R", 1, p)
                                        elif p >= t['sl']: engine.resolve_trade("SL HIT 🛑", "-1.0R", 0, p)
            except Exception:
                await asyncio.sleep(5)

    async def runner():
        engine.candles_data = get_historical_candles(500)
        await asyncio.gather(kline_listener(), trade_listener())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner())

if "worker_running" not in st.session_state:
    st.session_state["worker_running"] = True
    threading.Thread(target=run_worker_thread, daemon=True).start()

# --- 5. STREAMLIT FULL-SCREEN TRADING TERMINAL UI ---
st.set_page_config(page_title="BTCUSDT DYNAMIC TERMINAL", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #080a0f !important; }
        .stApp { background-color: #080a0f !important; }
        iframe { border: none !important; width: 100vw !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

# Safe Vault Fetch (Never Crashes Streamlit)
rows = []
try:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT timestamp, timeframe, direction, strategy_logic, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
except Exception:
    rows = []

total_trades = len(rows)
vault_json = json.dumps([{"time": r[0], "tf": r[1], "dir": r[2], "logic": r[3], "entry": r[4], "res": r[5], "pnl": r[6], "win": r[7]} for r in rows])

ui_candles = get_historical_candles(500)
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
            <span id="hud-badge-mode" class="badge-tf">15M • 1H • 48H</span>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">RADAR</span><div id="hud-status" class="c-yellow" style="font-weight:800; font-size:10px;">SCANNING</div></div>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">PRICE</span><div id="hud-live-price" class="c-cyan" style="font-weight:800; font-size:10px;">--</div></div>
            <div><span style="font-size:7px; color:#565f70; font-weight:700;">ACTIVE TF</span><div id="hud-tf" class="c-cyan" style="font-weight:800; font-size:10px;">CLEAN</div></div>
        </div>
        <button class="vault-btn" onclick="openVault()">📜 VAULT ({total_trades})</button>
    </div>

    <div id="chart-container"></div>

    <div class="bottom-section">
        <div class="bot-bar-1">
            <div id="status-line">SYSTEM: <span class="c-green">SCANNING 48H • 1H • 15M</span> (CLEAN SCREEN)</div>
            <div style="display:flex; gap:5px;">
                <span style="border:1px solid rgba(202,138,4,0.4); color:#fbbf24; padding:1px 4px; border-radius:2px; font-weight:700;">⚡ FORCE CLOSE</span>
                <span style="border:1px solid rgba(220,38,38,0.4); color:#f87171; padding:1px 4px; border-radius:2px; font-weight:700;">🚨 KILL SWITCH</span>
            </div>
        </div>
        <div class="bot-bar-2">
            <div class="info-card"><span style="color:#565f70;">ENGINE</span><span class="c-green">15M CASCADE</span></div>
            <div class="info-card"><span style="color:#565f70;">ACTIVE SETUP</span><span id="card-setup" class="c-yellow">WAITING SWEEP</span></div>
            <div class="info-card"><span style="color:#565f70;">FORMATS</span><span class="c-cyan">48H • 1H • 15M</span></div>
            <div class="info-card"><span style="color:#565f70;">SHIELD</span><span class="c-green">ARMED 🛡️</span></div>
        </div>
    </div>

    <div id="vaultModal" class="modal">
        <div class="modal-box">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1c2636; padding-bottom:8px; margin-bottom:8px;">
                <span style="font-weight:bold; font-size:12px;">🔒 TIMEFRAME EXECUTION VAULT</span>
                <span style="cursor:pointer; font-weight:bold;" onclick="closeVault()">✕</span>
            </div>
            <div id="vaultList" style="max-height: 250px; overflow-y: auto;"></div>
        </div>
    </div>

    <script>
        const rawCandles = {candles_json};
        const vaultTrades = {vault_json};
        let activeTrade = {active_trade_json};
        const localOffsetSeconds = 5.5 * 3600;

        let allCandles = rawCandles.map(c => ({{
            time: c.time + localOffsetSeconds,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close
        }}));

        let currentBar = allCandles.length > 0 ? {{ ...allCandles[allCandles.length - 1] }} : null;

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
                scaleMargins: {{ top: 0.12, bottom: 0.12 }}
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

        if (allCandles.length > 0) {{
            candleSeries.setData(allCandles);
            chart.timeScale().fitContent();
        }}

        let activeLines = {{ entry: null, sl: null, tp: null }};

        function renderDynamicTradeLines() {{
            if (activeLines.entry) candleSeries.removePriceLine(activeLines.entry);
            if (activeLines.sl) candleSeries.removePriceLine(activeLines.sl);
            if (activeLines.tp) candleSeries.removePriceLine(activeLines.tp);
            activeLines = {{ entry: null, sl: null, tp: null }};

            if (activeTrade) {{
                document.getElementById('hud-status').innerText = activeTrade.dir + " ARMED";
                document.getElementById('hud-status').className = activeTrade.dir === "LONG" ? "c-green" : "c-red";
                document.getElementById('hud-tf').innerText = activeTrade.tf;
                document.getElementById('status-line').innerHTML = `SETUP: <span class="c-cyan">${{activeTrade.tf}}</span> | <span class="c-green">${{activeTrade.logic}}</span>`;
                document.getElementById('card-setup').innerText = activeTrade.tf + " " + activeTrade.dir;

                activeLines.entry = candleSeries.createPriceLine({{
                    price: activeTrade.entry,
                    color: '#38bdf8',
                    lineWidth: 2,
                    lineStyle: LightweightCharts.LineStyle.Solid,
                    axisLabelVisible: true,
                    title: `ENTRY [${{activeTrade.tf}}]`
                }});

                activeLines.sl = candleSeries.createPriceLine({{
                    price: activeTrade.sl,
                    color: '#f43f5e',
                    lineWidth: 2,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: 'STOP LOSS'
                }});

                activeLines.tp = candleSeries.createPriceLine({{
                    price: activeTrade.tp,
                    color: '#10b981',
                    lineWidth: 2,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: `TARGET TP (${{activeTrade.rr}}R)`
                }});
            }} else {{
                document.getElementById('hud-status').innerText = "SCANNING";
                document.getElementById('hud-status').className = "c-yellow";
                document.getElementById('hud-tf').innerText = "CLEAN";
                document.getElementById('status-line').innerHTML = 'SYSTEM: <span class="c-green">SCANNING 48H • 1H • 15M</span> (NO CLUTTER)';
                document.getElementById('card-setup').innerText = "WAITING SWEEP";
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

                    const candleTime = Math.floor(k.t / 1000) + localOffsetSeconds;
                    currentBar = {{ time: candleTime, open: o, high: h, low: l, close: c }};
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
                            <span style="font-weight:bold; color:${{t.win === 1 ? '#089981' : '#f43f5e'}}">${{t.res}} (${{t.pnl}})</span>
                        </div>
                        <div style="color:#64748b; font-size:8.5px;">Logic: ${{t.logic}}</div>
                    </div>`;
            }});
        }}
        function closeVault() {{ document.getElementById('vaultModal').style.display = 'none'; }}
    </script>
</body>
</html>
"""

components.html(ui_html, height=490, scrolling=False)
