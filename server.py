import streamlit as st
import streamlit.components.v1 as components
import requests
import json
import sqlite3
from datetime import datetime

st.set_page_config(
    page_title="BTCUSDT RADAR 48H FUTURES",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Streamlit Chrome Elimination & Tight Viewport
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #080a0f !important; }
        .stApp { background-color: #080a0f !important; }
        iframe { border: none !important; width: 100vw !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

# Database Setup: SQLite Trade Vault
conn = sqlite3.connect('trades_vault.db', check_same_thread=False)
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

# Handle Trade Sync via URL Query Parameter
query_params = st.query_params
if "save_trade" in query_params:
    try:
        t_data = json.loads(query_params.get("save_trade"))
        cur.execute('''
            INSERT INTO vault (timestamp, direction, entry, sweep_level, atr, sl, tp, exit, result, pnl, is_win)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            t_data.get("timestamp"),
            t_data.get("direction"),
            float(t_data.get("entry")),
            float(t_data.get("sweep_level", 0.0)),
            float(t_data.get("atr", 0.0)),
            float(t_data.get("sl")),
            float(t_data.get("tp")),
            float(t_data.get("exit")),
            t_data.get("result"),
            t_data.get("pnl"),
            int(t_data.get("is_win"))
        ))
        conn.commit()
    except Exception:
        pass
    st.query_params.clear()

cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
rows = cur.fetchall()
total_trades = len(rows)
wins = sum(1 for r in rows if r[5] == 1)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

vault_json = json.dumps([
    {"time": r[0], "dir": r[1], "entry": r[2], "res": r[3], "pnl": r[4], "win": r[5]} for r in rows
])

# Fetch 650 5M Candles from Binance USDT-M Futures
def fetch_futures_buffer():
    urls = [
        "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit=650",
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=650"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.5).json()
            if isinstance(r, list) and len(r) >= 576:
                return [{"time": int(b[0]/1000), "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])} for b in r]
        except Exception:
            continue
    return []

candles = fetch_futures_buffer()
candles_json = json.dumps(candles)

# Native Engine with Dual WebSocket (Kline Stream + Live Price Stream)
custom_app_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ 
            background-color: #080a0f; 
            color: #d1d4dc; 
            overflow: hidden; 
            width: 100vw;
            display: flex; 
            flex-direction: column; 
        }}

        /* TOP HUD */
        .top-bar {{
            display: flex; 
            justify-content: space-between; 
            align-items: center;
            padding: 5px 8px; 
            background: #0d1118; 
            border-bottom: 1px solid rgba(255,255,255,0.06);
            height: 38px; 
            font-size: 10px; 
            flex-shrink: 0;
        }}
        .bar-left {{ display: flex; align-items: center; gap: 8px; }}
        .badge-live {{ background: #089981; color: #fff; font-size: 8px; font-weight: 800; padding: 2px 4px; border-radius: 2px; }}
        .badge-tf {{ background: #131924; color: #38bdf8; font-size: 8px; font-weight: 800; padding: 2px 5px; border-radius: 2px; border: 1px solid #1e293b; }}
        .hud-col {{ display: flex; flex-direction: column; }}
        .hud-lbl {{ font-size: 7px; color: #565f70; font-weight: 700; text-transform: uppercase; }}
        .hud-val {{ font-weight: 800; font-size: 10px; }}

        .vault-btn {{
            background: #141b27; border: 1px solid #232f42; color: #38bdf8;
            padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; cursor: pointer;
        }}

        .basis-badge {{
            position: absolute; top: 42px; left: 8px; z-index: 10;
            background: rgba(13, 17, 24, 0.9); border: 1px solid #232f42;
            padding: 3px 7px; border-radius: 4px; font-size: 8.5px; font-weight: 700;
            color: #64748b; pointer-events: none;
        }}

        #chart-container {{
            width: 100vw;
            height: 375px;
            position: relative;
        }}

        /* BOTTOM HUD */
        .bottom-section {{
            display: flex;
            flex-direction: column;
            width: 100vw;
            background: #080a0f;
            flex-shrink: 0;
        }}
        .bot-bar-1 {{
            display: flex; 
            justify-content: space-between; 
            align-items: center;
            padding: 4px 8px; 
            background: #0b0f16; 
            border-top: 1px solid rgba(255,255,255,0.06);
            height: 28px;
            font-size: 8.5px;
        }}
        .bot-bar-2 {{
            display: grid; 
            grid-template-columns: repeat(4, 1fr); 
            gap: 4px;
            padding: 3px 6px 5px 6px; 
            background: #06080c; 
            height: 34px;
            font-size: 7.5px;
        }}
        .info-card {{
            background: #0d121a; 
            border: 1px solid #161e2a; 
            padding: 2px 4px;
            border-radius: 3px; 
            display: flex; 
            flex-direction: column;
            justify-content: center;
        }}

        /* MODAL */
        .modal {{
            display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0,0,0,0.85); z-index: 999999; align-items: center; justify-content: center; padding: 16px;
        }}
        .modal-box {{
            background: #0f141e; border: 1px solid #1c2636; border-radius: 8px;
            width: 100%; max-width: 380px; padding: 14px;
        }}

        .c-green {{ color: #089981 !important; font-weight: bold; }}
        .c-red {{ color: #f23645 !important; font-weight: bold; }}
        .c-cyan {{ color: #00e5ff !important; font-weight: bold; }}
        .c-yellow {{ color: #f59e0b !important; font-weight: bold; }}
    </style>
</head>
<body>

    <div class="top-bar">
        <div class="bar-left">
            <span class="badge-live">FUTURES</span>
            <span class="badge-tf">5M • 48H</span>
            <div class="hud-col">
                <span class="hud-lbl">RADAR STATUS</span>
                <span id="hud-status" class="hud-val c-yellow">SCANNING</span>
            </div>
            <div class="hud-col">
                <span class="hud-lbl">ENTRY</span>
                <span id="hud-entry" class="hud-val c-cyan">--</span>
            </div>
            <div class="hud-col">
                <span class="hud-lbl">SL / TRAIL</span>
                <span id="hud-sl" class="hud-val c-red">--</span>
            </div>
            <div class="hud-col">
                <span class="hud-lbl">DIRECT TP</span>
                <span id="hud-tp" class="hud-val c-green">--</span>
            </div>
        </div>
        <button class="vault-btn" onclick="openVault()">📜 VAULT ({total_trades})</button>
    </div>

    <div id="basis-box" class="basis-badge">SCANNING: Awaiting 48H Sweep / Structure Shift</div>

    <div id="chart-container"></div>

    <div class="bottom-section">
        <div class="bot-bar-1">
            <div>ACCOUNT: <span class="c-green">$10.00 BASE</span> | ALLOCATION: <span class="c-cyan">$2.50 (10x FUTURES)</span></div>
            <div style="display:flex; gap:5px;">
                <span style="border:1px solid rgba(202,138,4,0.4); color:#fbbf24; padding:1px 4px; border-radius:2px; font-weight:700;">⚡ FORCE CLOSE</span>
                <span style="border:1px solid rgba(220,38,38,0.4); color:#f87171; padding:1px 4px; border-radius:2px; font-weight:700;">🚨 KILL SWITCH</span>
            </div>
        </div>
        <div class="bot-bar-2">
            <div class="info-card"><span style="color:#565f70;">48H BOUNDS</span><span id="card-bounds" class="c-cyan">CALCULATING</span></div>
            <div class="info-card"><span style="color:#565f70;">ATR (14)</span><span id="card-atr" class="c-yellow">--</span></div>
            <div class="info-card"><span style="color:#565f70;">DATA FEED</span><span class="c-green">FUTURES WS</span></div>
            <div class="info-card"><span style="color:#565f70;">SHIELD</span><span id="hud-shield" class="c-green">ARMED 🛡️</span></div>
        </div>
    </div>

    <div id="vaultModal" class="modal">
        <div class="modal-box">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1c2636; padding-bottom:8px; margin-bottom:8px;">
                <span style="font-weight:bold; font-size:12px;">🔒 SQLITE VAULT (PROTECTED TRADES)</span>
                <span style="cursor:pointer; font-weight:bold;" onclick="closeVault()">✕</span>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:10px; font-weight:bold; margin-bottom:8px;">
                <span>Total Protected: {total_trades}</span>
                <span>Win Rate: <span class="c-green">{win_rate}%</span></span>
            </div>
            <div id="vaultList" style="max-height: 220px; overflow-y: auto;"></div>
        </div>
    </div>

    <script>
        let candlesData = {candles_json};
        const vaultTrades = {vault_json};

        const container = document.getElementById('chart-container');
        const chart = LightweightCharts.createChart(container, {{
            layout: {{
                background: {{ type: 'solid', color: '#080a0f' }},
                textColor: '#64748b',
                fontSize: 10,
            }},
            grid: {{
                vertLines: {{ color: 'rgba(255, 255, 255, 0.03)' }},
                horzLines: {{ color: 'rgba(255, 255, 255, 0.03)' }},
            }},
            crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
            rightPriceScale: {{
                borderColor: '#161e2a',
                autoScale: true,
                scaleMargins: {{ top: 0.08, bottom: 0.02 }},
            }},
            timeScale: {{
                borderColor: '#161e2a',
                timeVisible: true,
                secondsVisible: false,
                barSpacing: 9,
                minBarSpacing: 2,
                fixLeftEdge: true,
                rightOffset: 4,
            }},
            handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true }},
            handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#089981',
            downColor: '#f23645',
            borderUpColor: '#089981',
            borderDownColor: '#f23645',
            wickUpColor: '#089981',
            wickDownColor: '#f23645',
        }});

        if (candlesData && candlesData.length > 0) {{
            candleSeries.setData(candlesData);
            chart.timeScale().fitContent();
        }}

        // State Machine Core Variables
        let state = "SCANNING"; // SCANNING | SWEEP_LOW_WAIT | SWEEP_HIGH_WAIT | ACTIVE_TRADE
        let sweepExtreme = null;
        let sweepLevel = null;
        let sweepCandleTime = null;
        let activeTrade = null;
        let tpLine = null, entryLine = null, slLine = null;

        // True Range ATR(14)
        function calculateATR(data, period = 14) {{
            if (data.length < period + 1) return 85.0;
            let trs = [];
            for (let i = data.length - period; i < data.length; i++) {{
                let hl = data[i].high - data[i].low;
                let hc = Math.abs(data[i].high - data[i - 1].close);
                let lc = Math.abs(data[i].low - data[i - 1].close);
                trs.push(Math.max(hl, hc, lc));
            }}
            let sum = trs.reduce((a, b) => a + b, 0);
            return sum / period;
        }}

        function get48HBounds(data) {{
            let slice = data.slice(-576);
            let h48 = Math.max(...slice.map(c => c.high));
            let l48 = Math.min(...slice.map(c => c.low));
            return {{ h48, l48 }};
        }}

        function updateBoundsUI() {{
            if (candlesData.length >= 576) {{
                const {{ h48, l48 }} = get48HBounds(candlesData);
                const atr = calculateATR(candlesData, 14);
                document.getElementById('card-bounds').innerText = `L:${{l48.toFixed(0)}} H:${{h48.toFixed(0)}}`;
                document.getElementById('card-atr').innerText = `${{atr.toFixed(1)}}`;
            }}
        }}
        updateBoundsUI();

        // 1. STRICT NEXT-CANDLE CONFIRMATION PIPELINE
        function onClosed5MCandle(closedCandle) {{
            candlesData.push(closedCandle);
            if (candlesData.length > 650) candlesData.shift();
            updateBoundsUI();

            const completedBuffer = candlesData.slice(0, -1);
            const {{ h48, l48 }} = get48HBounds(completedBuffer);
            const atr = calculateATR(completedBuffer, 14);

            if (state === "SCANNING") {{
                // Stage 1: Sweep Detection on Completed Bar
                if (closedCandle.low < l48) {{
                    state = "SWEEP_LOW_WAIT";
                    sweepExtreme = closedCandle.low;
                    sweepLevel = l48;
                    sweepCandleTime = closedCandle.time;
                    setShieldBadge("🛡️ 48H LOW SWEPT: WAITING NEXT CANDLE RECLAIM", "#f59e0b");
                }} else if (closedCandle.high > h48) {{
                    state = "SWEEP_HIGH_WAIT";
                    sweepExtreme = closedCandle.high;
                    sweepLevel = h48;
                    sweepCandleTime = closedCandle.time;
                    setShieldBadge("🛡️ 48H HIGH SWEPT: WAITING NEXT CANDLE REJECTION", "#f59e0b");
                }}
            }}
            else if (state === "SWEEP_LOW_WAIT") {{
                // Stage 2: Next Closed Candle Reclaim Confirmation
                if (closedCandle.time > sweepCandleTime) {{
                    if (closedCandle.close > sweepLevel) {{
                        let entry = closedCandle.close;
                        let sl = sweepExtreme - (1.6 * atr);
                        let risk = entry - sl;
                        let tp = entry + (2.5 * risk);
                        triggerTrade("LONG", entry, sl, tp, sweepLevel, atr);
                    }} else {{
                        if (closedCandle.low < sweepExtreme) {{
                            sweepExtreme = closedCandle.low; // lower wick extension
                        }} else {{
                            state = "SCANNING";
                            setShieldBadge("FAILED RECLAIM → SCANNING", "#64748b");
                        }}
                    }}
                }}
            }}
            else if (state === "SWEEP_HIGH_WAIT") {{
                // Stage 2: Next Closed Candle Rejection Confirmation
                if (closedCandle.time > sweepCandleTime) {{
                    if (closedCandle.close < sweepLevel) {{
                        let entry = closedCandle.close;
                        let sl = sweepExtreme + (1.6 * atr);
                        let risk = sl - entry;
                        let tp = entry - (2.5 * risk);
                        triggerTrade("SHORT", entry, sl, tp, sweepLevel, atr);
                    }} else {{
                        if (closedCandle.high > sweepExtreme) {{
                            sweepExtreme = closedCandle.high; // higher wick extension
                        }} else {{
                            state = "SCANNING";
                            setShieldBadge("FAILED REJECTION → SCANNING", "#64748b");
                        }}
                    }}
                }}
            }}
        }}

        function triggerTrade(dir, entry, sl, tp, sLevel, atrVal) {{
            state = "ACTIVE_TRADE";
            activeTrade = {{
                dir: dir,
                entry: entry,
                sl: sl,
                tp: tp,
                sweepLevel: sLevel,
                atr: atrVal,
                time: new Date().toLocaleTimeString([], {{hour: '2-digit', minute:'2-digit'}})
            }};

            document.getElementById('hud-status').innerText = "PRE-SIGNAL " + dir;
            document.getElementById('hud-status').className = dir === "LONG" ? "hud-val c-green" : "hud-val c-red";
            document.getElementById('hud-entry').innerText = "$" + entry.toFixed(1);
            document.getElementById('hud-sl').innerText = "$" + sl.toFixed(1);
            document.getElementById('hud-tp').innerText = "$" + tp.toFixed(1);

            const bText = dir === "LONG" 
                ? "🎯 CONFIRMED: 48H LOW SWEPT + RECLAIM CLOSED (RR 1:2.5)" 
                : "🛑 CONFIRMED: 48H HIGH SWEPT + REJECTION CLOSED (RR 1:2.5)";
            setShieldBadge(bText, "#00e5ff", dir === "LONG" ? "#089981" : "#f23645");

            clearLines();
            tpLine = candleSeries.createPriceLine({{ price: tp, color: '#089981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'DIRECT TP' }});
            entryLine = candleSeries.createPriceLine({{ price: entry, color: '#00e5ff', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'ENTRY' }});
            slLine = candleSeries.createPriceLine({{ price: sl, color: '#f23645', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'SL' }});
        }}

        function setShieldBadge(text, color, borderColor = "#232f42") {{
            const box = document.getElementById('basis-box');
            box.innerText = text;
            box.style.color = color;
            box.style.borderColor = borderColor;
        }}

        function clearLines() {{
            if (tpLine) {{ candleSeries.removePriceLine(tpLine); tpLine = null; }}
            if (entryLine) {{ candleSeries.removePriceLine(entryLine); entryLine = null; }}
            if (slLine) {{ candleSeries.removePriceLine(slLine); slLine = null; }}
        }}

        // 2 & 4. SEPARATE LIVE PRICE WEBSOCKET + AUTO-RECONNECT + RESOLVE TRADE
        let priceWs = null;
        let klineWs = null;

        function connectKlineWS() {{
            klineWs = new WebSocket('wss://fstream.binance.com/ws/btcusdt@kline_5m');
            klineWs.onmessage = (event) => {{
                const res = JSON.parse(event.data);
                const k = res.k;
                const candleUpdate = {{
                    time: Math.floor(k.t / 1000),
                    open: parseFloat(k.o),
                    high: parseFloat(k.h),
                    low: parseFloat(k.l),
                    close: parseFloat(k.c),
                    vol: parseFloat(k.v)
                }};
                candleSeries.update(candleUpdate);

                // STRICT: ONLY CLOSED BAR PROPAGATES
                if (k.x) {{
                    onClosed5MCandle(candleUpdate);
                }}
            }};
            klineWs.onclose = () => {{
                setTimeout(connectKlineWS, 2000);
            }};
        }}

        function connectLivePriceWS() {{
            // Dedicated Trade-By-Trade Stream for Millisecond TP/SL Detection
            priceWs = new WebSocket('wss://fstream.binance.com/ws/btcusdt@trade');
            priceWs.onmessage = (event) => {{
                const t = JSON.parse(event.data);
                const livePrice = parseFloat(t.p);

                // Instant Execution Check
                if (state === "ACTIVE_TRADE" && activeTrade) {{
                    if (activeTrade.dir === "LONG") {{
                        if (livePrice >= activeTrade.tp) resolveTrade("TP HIT 🔥", "+2.5R", 1, livePrice);
                        else if (livePrice <= activeTrade.sl) resolveTrade("SL HIT 🛑", "-1R", 0, livePrice);
                    }} else if (activeTrade.dir === "SHORT") {{
                        if (livePrice <= activeTrade.tp) resolveTrade("TP HIT 🔥", "+2.5R", 1, livePrice);
                        else if (livePrice >= activeTrade.sl) resolveTrade("SL HIT 🛑", "-1R", 0, livePrice);
                    }}
                }}
            }};
            priceWs.onclose = () => {{
                setTimeout(connectLivePriceWS, 2000);
            }};
        }}

        connectKlineWS();
        connectLivePriceWS();

        // COMPLETE RESOLVE TRADE + DATABASE PERSISTENCE SYNC
        function resolveTrade(result, pnl, isWin, exitPrice) {{
            const completedTrade = {{
                timestamp: activeTrade.time,
                direction: activeTrade.dir,
                entry: activeTrade.entry,
                sweep_level: activeTrade.sweepLevel,
                atr: activeTrade.atr,
                sl: activeTrade.sl,
                tp: activeTrade.tp,
                exit: exitPrice,
                result: result,
                pnl: pnl,
                is_win: isWin
            }};

            clearLines();
            vaultTrades.unshift({{
                time: completedTrade.timestamp,
                dir: completedTrade.direction,
                entry: completedTrade.entry.toFixed(1),
                res: result,
                pnl: pnl,
                win: isWin
            }});

            state = "SCANNING";
            activeTrade = null;
            document.getElementById('hud-status').innerText = "SCANNING";
            document.getElementById('hud-status').className = "hud-val c-yellow";
            document.getElementById('hud-entry').innerText = "--";
            document.getElementById('hud-sl').innerText = "--";
            document.getElementById('hud-tp').innerText = "--";
            setShieldBadge("SCANNING: Awaiting 48H Sweep / Structure Shift", "#64748b");

            // Sync into Streamlit SQLite Database silently without breaking canvas
            try {{
                const payload = encodeURIComponent(JSON.stringify(completedTrade));
                fetch(window.location.pathname + '?save_trade=' + payload, {{ method: 'GET' }});
            }} catch(e) {{}}
        }}

        // Vault UI
        function openVault() {{
            document.getElementById('vaultModal').style.display = 'flex';
            const list = document.getElementById('vaultList');
            list.innerHTML = '';
            if (vaultTrades.length === 0) {{
                list.innerHTML = '<div style="font-size:10px; color:#565f70; text-align:center; padding:15px;">No closed trades yet. Monitoring 48H sweeps...</div>';
            }} else {{
                vaultTrades.forEach(t => {{
                    const clr = t.win === 1 ? '#089981' : '#f23645';
                    list.innerHTML += `<div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #161e2a; font-size:10px;">
                        <span>${{t.time}} <b style="color:#00e5ff">${{t.dir}}</b> @ $${{t.entry}}</span>
                        <span style="color:${{clr}}; font-weight:bold;">${{t.res}} ${{t.pnl}}</span>
                    </div>`;
                }});
            }}
        }}
        function closeVault() {{
            document.getElementById('vaultModal').style.display = 'none';
        }}

        window.addEventListener('resize', () => {{
            chart.resize(container.clientWidth, container.clientHeight);
        }});
    </script>
</body>
</html>
"""

components.html(custom_app_html, height=490, scrolling=False)
