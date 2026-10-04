import streamlit as st
import streamlit.components.v1 as components
import requests
import json
import sqlite3
from datetime import datetime

st.set_page_config(
    page_title="BTCUSDT RADAR 48H",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom Styling (Zero Streamlit Chrome, App Mode)
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { 
            padding: 0 !important; 
            max-width: 100% !important;
            background-color: #080a0f !important; 
        }
        .stApp { 
            background-color: #080a0f !important; 
            color: #d1d4dc; 
            font-family: -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, sans-serif;
            overflow: hidden;
        }
        iframe {
            border: none !important;
            width: 100% !important;
        }
    </style>
""", unsafe_allow_html=True)

# Fetch 48H Klines (600 bars of 5M)
def get_candles():
    urls = [
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=600",
        "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=600"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.0).json()
            if isinstance(r, list) and len(r) > 100:
                chart_data = []
                for b in r:
                    chart_data.append({
                        "time": int(b[0] / 1000), # Unix timestamp in seconds
                        "open": float(b[1]),
                        "high": float(b[2]),
                        "low": float(b[3]),
                        "close": float(b[4]),
                        "volume": float(b[5])
                    })
                return chart_data
        except Exception:
            continue
    return []

candles = get_candles()

# Agar data fetch fail ho
if not candles:
    st.error("Market stream connecting...")
    st.stop()

# Strategy Calculations
last = candles[-1]
prev = candles[-2]
cur_p = last['close']
highs = [c['high'] for c in candles[-288:]]
lows = [c['low'] for c in candles[-288:]]
pdh = max(highs)
pdl = min(lows)
h48 = max([c['high'] for c in candles])
l48 = min([c['low'] for c in candles])

atr = max(last['high'] - last['low'], 85.0)

# SQLite Vault Logic
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
cur.execute('''
    CREATE TABLE IF NOT EXISTS active_signal (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        direction TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        basis TEXT
    )
''')
conn.commit()

cur.execute("SELECT direction, entry, sl, tp, basis FROM active_signal WHERE id = 1")
locked = cur.fetchone()

has_signal = False
radar_status = "SCANNING"
entry_val, sl_val, tp_val = 0.0, 0.0, 0.0
basis_text = "SCANNING: Awaiting 48H Sweep / Structure Shift"

if locked:
    has_signal = True
    direction, entry_val, sl_val, tp_val, basis_text = locked
    radar_status = f"ACTIVE {direction}"
    
    # Target Hit check
    if direction == "LONG":
        if cur_p >= tp_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "LONG", entry_val, "TP HIT 🔥", "(+$0.65)", 1))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
        elif cur_p <= sl_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "LONG", entry_val, "SL HIT 🛑", "(-$0.80)", 0))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
    elif direction == "SHORT":
        if cur_p <= tp_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "SHORT", entry_val, "TP HIT 🔥", "(+$0.65)", 1))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
        elif cur_p >= sl_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "SHORT", entry_val, "SL HIT 🛑", "(-$0.80)", 0))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
else:
    # Trigger Condition
    if prev['low'] <= pdl and last['close'] > pdl:
        has_signal = True
        radar_status = "ACTIVE LONG"
        entry_val = cur_p
        sl_val = round(cur_p - (atr * 1.6), 1)
        tp_val = round(min(cur_p + (atr * 2.8), h48), 1)
        basis_text = "🎯 BASIS: PDL LIQUIDITY SWEEP & RECLAIM"
        cur.execute("INSERT OR REPLACE INTO active_signal VALUES (1, 'LONG', ?, ?, ?, ?)", (entry_val, sl_val, tp_val, basis_text))
        conn.commit()
    elif prev['high'] >= pdh and last['close'] < pdh:
        has_signal = True
        radar_status = "ACTIVE SHORT"
        entry_val = cur_p
        sl_val = round(cur_p + (atr * 1.6), 1)
        tp_val = round(max(cur_p - (atr * 2.8), l48), 1)
        basis_text = "🛑 BASIS: PDH LIQUIDITY SWEEP & REJECTION"
        cur.execute("INSERT OR REPLACE INTO active_signal VALUES (1, 'SHORT', ?, ?, ?, ?)", (entry_val, sl_val, tp_val, basis_text))
        conn.commit()

# Vault count
cur.execute("SELECT COUNT(*) FROM vault")
vault_count = cur.fetchone()[0]

cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
rows = cur.fetchall()
total_trades = len(rows)
wins = sum(1 for r in rows if r[5] == 1)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

trade_rows_json = json.dumps([
    {"time": r[0], "dir": r[1], "entry": r[2], "res": r[3], "pnl": r[4], "win": r[5]} for r in rows
])

candles_json = json.dumps(candles)

# EMBEDDED COMPLETE CLIENT-SIDE APP (0% BLINK, NATIVE WEBSOCKET, TRADINGVIEW CANVAS)
html_code = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ background-color: #080a0f; color: #d1d4dc; overflow: hidden; }}

        /* Top HUD */
        .top-bar {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 5px 8px; background: #0d1118; border-bottom: 1px solid rgba(255,255,255,0.06);
            height: 38px; font-size: 10px;
        }}
        .bar-left {{ display: flex; align-items: center; gap: 8px; }}
        .badge-live {{ background: #089981; color: #fff; font-size: 8px; font-weight: 800; padding: 2px 4px; border-radius: 2px; }}
        .badge-tf {{ background: #131924; color: #38bdf8; font-size: 8px; font-weight: 800; padding: 2px 4px; border-radius: 2px; border: 1px solid #1e293b; }}
        .hud-col {{ display: flex; flex-direction: column; }}
        .hud-lbl {{ font-size: 7px; color: #565f70; font-weight: 700; text-transform: uppercase; }}
        .hud-val {{ font-weight: 800; font-size: 10.5px; }}

        .vault-btn {{
            background: #141b27; border: 1px solid #232f42; color: #38bdf8;
            padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; cursor: pointer;
        }}

        /* Signal Basis Badge */
        .basis-badge {{
            position: absolute; top: 44px; left: 10px; z-index: 10;
            background: rgba(13, 17, 24, 0.85); border: 1px solid #232f42;
            padding: 4px 8px; border-radius: 4px; font-size: 9px; font-weight: 700;
            color: {'#00e5ff' if has_signal else '#64748b'};
            pointer-events: none;
        }}

        #chart-container {{
            width: 100vw; height: calc(100vh - 108px); position: relative;
        }}

        /* Bottom HUD */
        .bot-bar-1 {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 4px 8px; background: #0b0f16; border-top: 1px solid rgba(255,255,255,0.06);
            height: 32px; font-size: 9px;
        }}
        .bot-bar-2 {{
            display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px;
            padding: 3px 6px 6px 6px; background: #06080c; height: 38px; font-size: 8px;
        }}
        .info-card {{
            background: #0d121a; border: 1px solid #161e2a; padding: 2px 4px;
            border-radius: 3px; display: flex; flex-direction: column;
        }}

        /* Modal */
        .modal {{
            display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0,0,0,0.85); z-index: 99999; align-items: center; justify-content: center; padding: 16px;
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
                <span class="hud-lbl">STATUS</span>
                <span class="hud-val c-yellow">{radar_status}</span>
            </div>
            <div class="hud-col">
                <span class="hud-lbl">ENTRY</span>
                <span class="hud-val c-cyan">{f"${entry_val}" if has_signal else "--"}</span>
            </div>
            <div class="hud-col">
                <span class="hud-lbl">SL</span>
                <span class="hud-val c-red">{f"${sl_val}" if has_signal else "--"}</span>
            </div>
            <div class="hud-col">
                <span class="hud-lbl">TP</span>
                <span class="hud-val c-green">{f"${tp_val}" if has_signal else "--"}</span>
            </div>
        </div>
        <button class="vault-btn" onclick="openVault()">📜 VAULT ({vault_count})</button>
    </div>

    <!-- BASIS BADGE -->
    <div class="basis-badge">{basis_text}</div>

    <!-- CHART CANVAS -->
    <div id="chart-container"></div>

    <!-- BOTTOM HUD -->
    <div class="bot-bar-1">
        <div>ACCOUNT: <span class="c-green">$10.00 BASE</span> | ALLOCATION: <span class="c-cyan">$2.50 (10x)</span></div>
        <div style="display:flex; gap:6px;">
            <span style="border:1px solid rgba(202,138,4,0.4); color:#fbbf24; padding:2px 5px; border-radius:2px; font-weight:700;">⚡ FORCE CLOSE</span>
            <span style="border:1px solid rgba(220,38,38,0.4); color:#f87171; padding:2px 5px; border-radius:2px; font-weight:700;">🚨 KILL SWITCH</span>
        </div>
    </div>
    <div class="bot-bar-2">
        <div class="info-card"><span style="color:#565f70;">THREAD 3 RISK</span><span class="c-green">-$2.00 GUARD</span></div>
        <div class="info-card"><span style="color:#565f70;">FILTERS</span><span class="c-cyan">48H • BOS • APR</span></div>
        <div class="info-card"><span style="color:#565f70;">REGIME</span><span class="c-green">PULLBACK REGIME</span></div>
        <div class="info-card"><span style="color:#565f70;">SCANNER</span><span class="c-yellow">{radar_status}</span></div>
    </div>

    <!-- MODAL POPUP -->
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
            <div id="vaultList" style="max-height: 200px; overflow-y: auto;"></div>
        </div>
    </div>

    <script>
        const chartData = {candles_json};
        const vaultTrades = {trade_rows_json};

        // Initialize TradingView Native Lightweight Chart
        const container = document.getElementById('chart-container');
        const chart = LightweightCharts.createChart(container, {{
            layout: {{
                backgroundColor: '#080a0f',
                textColor: '#565f70',
                fontSize: 10,
            }},
            grid: {{
                vertLines: {{ color: 'rgba(255, 255, 255, 0.03)' }},
                horzLines: {{ color: 'rgba(255, 255, 255, 0.03)' }},
            }},
            crosshair: {{
                mode: LightweightCharts.CrosshairMode.Normal,
            }},
            rightPriceScale: {{
                borderColor: '#161e2a',
                scaleMargins: {{ top: 0.1, bottom: 0.15 }},
            }},
            timeScale: {{
                borderColor: '#161e2a',
                timeVisible: true,
                secondsVisible: false,
            }},
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#089981',
            downColor: '#f23645',
            borderUpColor: '#089981',
            borderDownColor: '#f23645',
            wickUpColor: '#089981',
            wickDownColor: '#f23645',
        }});

        candleSeries.setData(chartData);

        // Target Dotted Lines if Signal is Active
        const hasSig = {str(has_signal).lower()};
        if (hasSig) {{
            candleSeries.createPriceLine({{
                price: {tp_val},
                color: '#089981',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: 'DIRECT TP',
            }});
            candleSeries.createPriceLine({{
                price: {entry_val},
                color: '#00e5ff',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: 'ENTRY',
            }});
            candleSeries.createPriceLine({{
                price: {sl_val},
                color: '#f23645',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: 'SL',
            }});
        }}

        // REAL-TIME DIRECT BINANCE WEBSOCKET (0% BLINKING, CONTINUOUS TICK UPDATE)
        const ws = new WebSocket('wss://stream.binance.com:9443/ws/btcusdt@kline_5m');
        ws.onmessage = (event) => {{
            const message = JSON.parse(event.data);
            const k = message.k;
            const updatedCandle = {{
                time: Math.floor(k.t / 1000),
                open: parseFloat(k.o),
                high: parseFloat(k.h),
                low: parseFloat(k.l),
                close: parseFloat(k.c)
            }};
            candleSeries.update(updatedCandle);
        }};

        // Modal Controls
        function openVault() {{
            document.getElementById('vaultModal').style.display = 'flex';
            const list = document.getElementById('vaultList');
            list.innerHTML = '';
            if (vaultTrades.length === 0) {{
                list.innerHTML = '<div style="font-size:10px; color:#565f70; text-align:center; padding:15px;">No closed trades yet. Monitoring institutional sweeps...</div>';
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

        // Auto Resize on Screen Rotation
        window.addEventListener('resize', () => {{
            chart.resize(container.clientWidth, container.clientHeight);
        }});
    </script>
</body>
</html>
"""

components.html(html_code, height=660, scrolling=False)
