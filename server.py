import streamlit as st
import streamlit.components.v1 as components
import requests
import json
import sqlite3

st.set_page_config(
    page_title="BTCUSDT RADAR 48H",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Streamlit chrome aur blank space ko zero lock karein
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #080a0f !important; }
        .stApp { background-color: #080a0f !important; overflow: hidden !important; }
        iframe { border: none !important; width: 100vw !important; height: 100vh !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

# Database
conn = sqlite3.connect('trades_vault.db', check_same_thread=False)
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS vault (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        direction TEXT,
        entry REAL,
        result TEXT,
        pnl TEXT,
        is_win INTEGER
    )
''')
conn.commit()

cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
rows = cur.fetchall()
total_trades = len(rows)
wins = sum(1 for r in rows if r[5] == 1)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

vault_json = json.dumps([
    {"time": r[0], "dir": r[1], "entry": r[2], "res": r[3], "pnl": r[4], "win": r[5]} for r in rows
])

def fetch_candles():
    url = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=350"
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.5).json()
        if isinstance(r, list) and len(r) > 20:
            return [{"time": int(b[0]/1000), "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4])} for b in r]
    except Exception:
        pass
    try:
        r = requests.get("https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=350", timeout=3.5).json()
        return [{"time": int(b[0]/1000), "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4])} for b in r]
    except Exception:
        return []

candles = fetch_candles()
candles_json = json.dumps(candles)

custom_app_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        html, body {{ 
            background-color: #080a0f; 
            color: #d1d4dc; 
            overflow: hidden; 
            height: 100vh; 
            width: 100vw;
            display: flex; 
            flex-direction: column; 
        }}

        /* TOP HUD BAR */
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

        /* CHART CONTAINER: Fills exact screen height between top and bottom bars */
        #chart-container {{
            flex: 1;
            width: 100vw;
            position: relative;
            min-height: 0;
        }}

        /* BOTTOM SECTION (TIGHTLY ATTACHED TO BOTTOM OF CHART) */
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

    <!-- TOP HUD -->
    <div class="top-bar">
        <div class="bar-left">
            <span class="badge-live">LIVE</span>
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

    <!-- BASIS BADGE -->
    <div id="basis-box" class="basis-badge">SCANNING: Awaiting 48H Sweep / Structure Shift</div>

    <!-- MAIN CHART AREA -->
    <div id="chart-container"></div>

    <!-- BOTTOM CONTROLS & SCANNER PANELS (TIGHTLY ATTACHED) -->
    <div class="bottom-section">
        <div class="bot-bar-1">
            <div>ACCOUNT: <span class="c-green">$10.00 BASE</span> | ALLOCATION: <span class="c-cyan">$2.50 (10x)</span></div>
            <div style="display:flex; gap:5px;">
                <span style="border:1px solid rgba(202,138,4,0.4); color:#fbbf24; padding:1px 4px; border-radius:2px; font-weight:700;">⚡ FORCE CLOSE</span>
                <span style="border:1px solid rgba(220,38,38,0.4); color:#f87171; padding:1px 4px; border-radius:2px; font-weight:700;">🚨 KILL SWITCH</span>
            </div>
        </div>
        <div class="bot-bar-2">
            <div class="info-card"><span style="color:#565f70;">THREAD 3 RISK</span><span class="c-green">-$2.00 GUARD</span></div>
            <div class="info-card"><span style="color:#565f70;">FILTERS</span><span class="c-cyan">48H • BOS • APR</span></div>
            <div class="info-card"><span style="color:#565f70;">REGIME</span><span id="hud-regime" class="c-green">PULLBACK REGIME</span></div>
            <div class="info-card"><span style="color:#565f70;">SCANNER</span><span id="hud-scanner" class="c-yellow">SCANNING</span></div>
        </div>
    </div>

    <!-- VAULT MODAL -->
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
        const candlesData = {candles_json};
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

        let activeSignal = null;
        let tpLine = null, entryLine = null, slLine = null;

        function checkStrategyRules(curCandle, prevCandle) {{
            if (!candlesData || candlesData.length < 50) return;
            let h48 = Math.max(...candlesData.map(c => c.high));
            let l48 = Math.min(...candlesData.map(c => c.low));
            let atr = Math.max(curCandle.high - curCandle.low, 80.0);

            if (!activeSignal) {{
                if (prevCandle.low <= l48 && curCandle.close > l48) {{
                    armSignal("LONG", curCandle.close, curCandle.close - (atr * 1.6), curCandle.close + (atr * 2.5), "🎯 BASIS: 48H LOW LIQUIDITY SWEEP & RECLAIM");
                }} else if (prevCandle.high >= h48 && curCandle.close < h48) {{
                    armSignal("SHORT", curCandle.close, curCandle.close + (atr * 1.6), curCandle.close - (atr * 2.5), "🛑 BASIS: 48H HIGH BUY-SIDE LIQUIDITY SWEEP");
                }}
            }} else {{
                if (activeSignal.dir === "LONG") {{
                    if (curCandle.close >= activeSignal.tp || curCandle.close <= activeSignal.sl) clearSignalLines();
                }} else if (activeSignal.dir === "SHORT") {{
                    if (curCandle.close <= activeSignal.tp || curCandle.close >= activeSignal.sl) clearSignalLines();
                }}
            }}
        }}

        function armSignal(direction, entry, sl, tp, basis) {{
            activeSignal = {{ dir: direction, entry: entry.toFixed(1), sl: sl.toFixed(1), tp: tp.toFixed(1) }};
            document.getElementById('hud-status').innerText = "PRE-SIGNAL " + direction;
            document.getElementById('hud-status').className = direction === "LONG" ? "hud-val c-green" : "hud-val c-red";
            document.getElementById('hud-scanner').innerText = "ACTIVE " + direction;
            document.getElementById('hud-entry').innerText = "$" + activeSignal.entry;
            document.getElementById('hud-sl').innerText = "$" + activeSignal.sl;
            document.getElementById('hud-tp').innerText = "$" + activeSignal.tp;

            const basisEl = document.getElementById('basis-box');
            basisEl.innerText = basis;
            basisEl.style.color = "#00e5ff";
            basisEl.style.borderColor = direction === "LONG" ? "#089981" : "#f23645";

            clearSignalLines();
            tpLine = candleSeries.createPriceLine({{ price: parseFloat(activeSignal.tp), color: '#089981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'DIRECT TP' }});
            entryLine = candleSeries.createPriceLine({{ price: parseFloat(activeSignal.entry), color: '#00e5ff', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'ENTRY' }});
            slLine = candleSeries.createPriceLine({{ price: parseFloat(activeSignal.sl), color: '#f23645', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'SL' }});
        }}

        function clearSignalLines() {{
            if (tpLine) {{ candleSeries.removePriceLine(tpLine); tpLine = null; }}
            if (entryLine) {{ candleSeries.removePriceLine(entryLine); entryLine = null; }}
            if (slLine) {{ candleSeries.removePriceLine(slLine); slLine = null; }}
            activeSignal = null;
            document.getElementById('hud-status').innerText = "SCANNING";
            document.getElementById('hud-status').className = "hud-val c-yellow";
            document.getElementById('hud-scanner').innerText = "SCANNING";
            document.getElementById('hud-entry').innerText = "--";
            document.getElementById('hud-sl').innerText = "--";
            document.getElementById('hud-tp').innerText = "--";
            document.getElementById('basis-box').innerText = "SCANNING: Awaiting 48H Sweep / Structure Shift";
            document.getElementById('basis-box').style.color = "#64748b";
            document.getElementById('basis-box').style.borderColor = "#232f42";
        }}

        // LIVE REAL-TIME BINANCE WEBSOCKET
        const ws = new WebSocket('wss://stream.binance.com:9443/ws/btcusdt@kline_5m');
        ws.onmessage = (event) => {{
            const res = JSON.parse(event.data);
            const k = res.k;
            const updated = {{
                time: Math.floor(k.t / 1000),
                open: parseFloat(k.o),
                high: parseFloat(k.h),
                low: parseFloat(k.l),
                close: parseFloat(k.c)
            }};
            candleSeries.update(updated);
            if (candlesData.length > 0) {{
                checkStrategyRules(updated, candlesData[candlesData.length - 1]);
            }}
        }};

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

        // Screen change par auto fit resize
        window.addEventListener('resize', () => {{
            chart.resize(container.clientWidth, container.clientHeight);
        }});
    </script>
</body>
</html>
"""

components.html(custom_app_html, height=720, scrolling=False)
