import os
import json
import requests
import streamlit as st
import streamlit.components.v1 as components

# ============================================================
# BTCUSDT SMC PRO DASHBOARD (FIXED LIVE TICK CANDLE ENGINE)
# ============================================================

st.set_page_config(
    page_title="BTCUSDT SMC PRO",
    layout="wide",
    initial_sidebar_state="collapsed"
)

SYMBOL = "BTCUSDT"
RENDER_URL = "https://btc-live-terminal-2.onrender.com"

# --- 1. FETCH DIRECT STRUCTURED JSON FROM RENDER ---
def get_render_sync():
    try:
        r = requests.get(f"{RENDER_URL}/vault-data", timeout=3.5)
        if r.status_code == 200:
            data = r.json()
            return data.get("vault", []), data.get("active", [])
    except Exception:
        pass
    return [], []

# --- 2. FETCH BINANCE CANDLES ---
def fetch_initial_candles(interval="15m", limit=200):
    urls = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3).json()
            if isinstance(r, list) and len(r) > 20:
                # Include all candles including current one
                return [{
                    "time": int(b[0] // 1000),
                    "open": float(b[1]),
                    "high": float(b[2]),
                    "low": float(b[3]),
                    "close": float(b[4])
                } for b in r]
        except Exception:
            continue
    return []

vault_list, active_list = get_render_sync()

total_trades = len(vault_list)
wins = sum(1 for r in vault_list if r.get("pnl", 0.0) > 0)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0
net_r = sum(r.get("pnl", 0.0) for r in vault_list) if total_trades > 0 else 0.0

vault_json = json.dumps(vault_list)
active_json = json.dumps(active_list)
candles = fetch_initial_candles("15m", 200)
candles_json = json.dumps(candles)

# --- 3. CSS FULLSCREEN SETUP ---
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #06080d !important; }
        .stApp { background-color: #06080d !important; }
        iframe { border: none !important; width: 100vw !important; height: 100vh !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

# --- 4. TRADINGVIEW LIGHTWEIGHT CHARTS WITH REAL-TIME TICK SYNC ---
ui_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ background:#06080d; color:#c3c7d1; overflow:hidden; width:100vw; height:100vh; display:flex; flex-direction:column; }}
        .nav {{ height:38px; background:#0c1017; border-bottom:1px solid #161e2a; display:flex; justify-content:space-between; align-items:center; padding:0 10px; font-size:11px; }}
        #chart-wrap {{ flex:1; width:100vw; height:calc(100vh - 38px); position:relative; background:#06080d; }}
        .btn {{ background:#16202f; border:1px solid #233147; color:#38bdf8; padding:4px 9px; border-radius:4px; font-size:10px; cursor:pointer; font-weight:bold; }}
        .btn:hover {{ background:#202d42; }}
        .modal {{ display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.85); z-index:999; align-items:center; justify-content:center; padding:12px; }}
        .box {{ background:#0b0f16; border:1px solid #1c2636; border-radius:8px; width:100%; max-width:460px; padding:14px; max-height:85vh; overflow-y:auto; }}
    </style>
</head>
<body>
    <div class="nav">
        <div style="display:flex; align-items:center; gap:6px;">
            <b style="color:#f59e0b;">BTCUSDT</b>
            <span style="background:rgba(8,153,129,0.2); color:#089981; font-size:8px; padding:1px 4px; border-radius:2px; font-weight:bold;">● LIVE 15M</span>
            <span id="active-tag" style="color:#38bdf8; font-size:9.5px; font-weight:600;">SCANNING</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
            <span id="price-txt" style="color:#089981; font-weight:bold;">CONNECTING...</span>
            <button class="btn" onclick="toggleVault()">📜 VAULT ({total_trades})</button>
        </div>
    </div>
    
    <div id="chart-wrap"></div>

    <div id="vaultModal" class="modal">
        <div class="box">
            <div style="display:flex; justify-content:space-between; margin-bottom:8px; border-bottom:1px solid #1c2636; padding-bottom:6px;">
                <b style="color:#f8fafc; font-size:11px;">VAULT HISTORY ({total_trades} TRADES | NET: {net_r:+.1f}R)</b>
                <span style="cursor:pointer; color:#94a3b8;" onclick="toggleVault()">✕</span>
            </div>
            <div id="vaultContent" style="font-size:10px;"></div>
        </div>
    </div>

    <script>
        const initialCandles = {candles_json};
        const activeTrades = {active_json};
        const vault = {vault_json};

        const el = document.getElementById('chart-wrap');
        const chart = LightweightCharts.createChart(el, {{
            layout: {{ background: {{ type: 'solid', color: '#06080d' }}, textColor: '#787f8f', fontSize: 10 }},
            grid: {{ vertLines: {{ color: 'rgba(255,255,255,0.02)' }}, horzLines: {{ color: 'rgba(255,255,255,0.02)' }} }},
            rightPriceScale: {{ borderColor: '#161e2a', autoScale: true }},
            timeScale: {{ borderColor: '#161e2a', timeVisible: true, secondsVisible: false, barSpacing: 8 }}
        }});

        const series = chart.addCandlestickSeries({{
            upColor: '#089981', downColor: '#f23645',
            borderUpColor: '#089981', borderDownColor: '#f23645',
            wickUpColor: '#089981', wickDownColor: '#f23645'
        }});

        let currentBar = null;

        if (initialCandles && initialCandles.length > 0) {{
            series.setData(initialCandles);
            currentBar = {{ ...initialCandles[initialCandles.length - 1] }};
        }}

        // Render Active Trade Levels
        if (activeTrades && activeTrades.length > 0) {{
            let tags = [];
            activeTrades.forEach(t => {{
                tags.push(`[${{t.tf}}] ${{t.dir}} @ ${{t.entry.toFixed(0)}}`);
                series.createPriceLine({{ price: t.entry, color: '#38bdf8', lineWidth: 1.5, title: 'ENTRY' }});
                series.createPriceLine({{ price: t.sl, color: '#f43f5e', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'SL' }});
                series.createPriceLine({{ price: t.tp3, color: '#10b981', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'TP3' }});
            }});
            document.getElementById('active-tag').innerText = "• ACTIVE: " + tags.join(" | ");
        }}

        // LIVE DIRECT BINANCE 15M KLINE WEBSOCKET
        const wsKline = new WebSocket('wss://fstream.binance.com/ws/btcusdt@kline_15m');
        wsKline.onmessage = (event) => {{
            try {{
                const data = JSON.parse(event.data);
                const k = data.k;
                const candleTime = Math.floor(k.t / 1000);
                const open = parseFloat(k.o);
                const high = parseFloat(k.h);
                const low = parseFloat(k.l);
                const close = parseFloat(k.c);

                currentBar = {{
                    time: candleTime,
                    open: open,
                    high: high,
                    low: low,
                    close: close
                }};

                // Real-time update to candlestick
                series.update(currentBar);

                // Update navbar price instantly
                const priceEl = document.getElementById('price-txt');
                priceEl.innerText = '$' + close.toLocaleString('en-US', {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
                priceEl.style.color = close >= open ? '#089981' : '#f23645';
            }} catch(err) {{
                console.error(err);
            }}
        }};

        // Fallback Trade Ticker for sub-second micro ticks
        const wsTrade = new WebSocket('wss://fstream.binance.com/ws/btcusdt@trade');
        wsTrade.onmessage = (event) => {{
            try {{
                const t = JSON.parse(event.data);
                const p = parseFloat(t.p);
                if (currentBar && p > 10000) {{
                    currentBar.close = p;
                    if (p > currentBar.high) currentBar.high = p;
                    if (p < currentBar.low) currentBar.low = p;
                    series.update(currentBar);
                }}
            }} catch(err) {{}}
        }};

        function toggleVault() {{
            const m = document.getElementById('vaultModal');
            m.style.display = m.style.display === 'flex' ? 'none' : 'flex';
            if (m.style.display === 'flex') {{
                let h = '';
                vault.forEach(v => {{
                    const pnlColor = v.pnl >= 0 ? '#089981' : '#f43f5e';
                    const sign = v.pnl >= 0 ? '+' : '';
                    h += `<div style="padding:6px 0; border-bottom:1px solid #161e2a;">
                        <div style="display:flex; justify-content:space-between;">
                            <b>[${{v.tf}}] ${{v.dir}}</b> @ ${{v.entry ? v.entry.toFixed(1) : '-'}}
                            <span style="color:${{pnlColor}}; font-weight:bold;">${{v.res || v.status}} (${{sign}}${{v.pnl}}R)</span>
                        </div>
                        <div style="color:#64748b; font-size:9px;">Setup: ${{v.setup}} | Status: <b>${{v.status}}</b></div>
                    </div>`;
                }});
                document.getElementById('vaultContent').innerHTML = h || '<div style="color:#64748b; padding:10px 0;">No trades recorded yet.</div>';
            }}
        }}

        window.addEventListener('resize', () => chart.applyOptions({{ width: el.clientWidth, height: el.clientHeight }}));
    </script>
</body>
</html>
"""

components.html(ui_html, height=880, scrolling=False)
