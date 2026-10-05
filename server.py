import os
import json
import time
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="BTCUSDT SMC PRO",
    layout="wide",
    initial_sidebar_state="collapsed"
)

SYMBOL = "BTCUSDT"
RENDER_URL = "https://btc-live-terminal-2.onrender.com"

# 1. Fetch from Render
def get_render_sync():
    try:
        r = requests.get(f"{RENDER_URL}/vault-data", timeout=3)
        if r.status_code == 200:
            d = r.json()
            return d.get("vault", []), d.get("active", [])
    except Exception:
        pass
    return [], []

# 2. Fetch Binance Candles with Robust Fallback
def get_candles(interval="15m", limit=120):
    urls = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=3).json()
            if isinstance(r, list) and len(r) > 10:
                return [{
                    "time": int(b[0] // 1000),
                    "open": float(b[1]),
                    "high": float(b[2]),
                    "low": float(b[3]),
                    "close": float(b[4])
                } for b in r]
        except Exception:
            continue

    # Synthetic fallback if all APIs are unreachable
    now = int(time.time())
    base_p = 85000.0
    return [{
        "time": now - (i * 900),
        "open": base_p,
        "high": base_p + 100.0,
        "low": base_p - 100.0,
        "close": base_p
    } for i in range(50, -1, -1)]

vault_list, active_list = get_render_sync()
candles = get_candles("15m", 120)

total_trades = len(vault_list)
net_r = sum(float(r.get("pnl", 0) or 0) for r in vault_list)

vault_json = json.dumps(vault_list)
active_json = json.dumps(active_list)
candles_json = json.dumps(candles)

# Clean CSS - No height clash
st.markdown("""
    <style>
    header, footer, #MainMenu { display: none !important; }
    .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background: #06080d !important; }
    .stApp { background: #06080d !important; }
    iframe { width: 100% !important; height: 95vh !important; border: none !important; }
    </style>
""", unsafe_allow_html=True)

HTML_PAYLOAD = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
html, body {{ width:100%; height:100%; background:#06080d; color:#c3c7d1; overflow:hidden; }}
#layout {{ width:100%; height:100%; display:flex; flex-direction:column; position:absolute; top:0; left:0; }}
.nav {{ height:44px; min-height:44px; background:#0c1017; border-bottom:1px solid #161e2a; display:flex; justify-content:space-between; align-items:center; padding:0 12px; font-size:11px; z-index:10; }}
#chart-container {{ flex:1; width:100%; min-height:300px; position:relative; background:#06080d; }}
.btn {{ background:#16202f; border:1px solid #233147; color:#38bdf8; padding:5px 10px; border-radius:4px; font-size:10px; cursor:pointer; font-weight:bold; }}
.modal {{ display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.85); z-index:999; align-items:center; justify-content:center; padding:15px; }}
.box {{ background:#0b0f16; border:1px solid #1c2636; border-radius:8px; width:100%; max-width:440px; padding:15px; max-height:80vh; overflow-y:auto; }}
</style>
</head>
<body>

<div id="layout">
    <div class="nav">
        <div style="display:flex; align-items:center; gap:6px;">
            <b style="color:#f59e0b; font-size:12px;">BTCUSDT</b>
            <span style="background:rgba(8,153,129,0.2); color:#089981; font-size:9px; padding:2px 4px; border-radius:3px; font-weight:bold;">● 15M LIVE</span>
            <span id="active-tag" style="color:#38bdf8; font-size:9px; font-weight:600;">SCANNING</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
            <span id="price-txt" style="color:#089981; font-weight:bold; font-size:12px;">CONNECTING...</span>
            <button class="btn" onclick="toggleVault()">📜 VAULT (<span id="vault-count">{total_trades}</span>)</button>
        </div>
    </div>
    <div id="chart-container"></div>
</div>

<div id="vaultModal" class="modal">
    <div class="box">
        <div style="display:flex; justify-content:space-between; margin-bottom:10px; border-bottom:1px solid #1c2636; padding-bottom:6px;">
            <b style="color:#f8fafc; font-size:11px;">VAULT HISTORY ({total_trades} TRADES | NET: {net_r:+.1f}R)</b>
            <span style="cursor:pointer; color:#94a3b8; font-size:14px; font-weight:bold;" onclick="toggleVault()">✕</span>
        </div>
        <div id="vaultContent" style="font-size:10px;"></div>
    </div>
</div>

<script>
let initialCandles = {candles_json};
let activeTrades = {active_json};
let vault = {vault_json};

const container = document.getElementById("chart-container");
const chart = LightweightCharts.createChart(container, {{
    width: container.clientWidth || window.innerWidth,
    height: container.clientHeight || (window.innerHeight - 44),
    layout: {{ background: {{ type: "solid", color: "#06080d" }}, textColor: "#787f8f", fontSize: 10 }},
    grid: {{ vertLines: {{ color: "rgba(255,255,255,0.02)" }}, horzLines: {{ color: "rgba(255,255,255,0.02)" }} }},
    rightPriceScale: {{ borderColor: "#161e2a", autoScale: true }},
    timeScale: {{ borderColor: "#161e2a", timeVisible: true, secondsVisible: false, barSpacing: 8 }}
}});

const series = chart.addCandlestickSeries({{
    upColor: "#089981", downColor: "#f23645",
    borderUpColor: "#089981", borderDownColor: "#f23645",
    wickUpColor: "#089981", wickDownColor: "#f23645"
}});

let currentBar = null;
let priceLines = [];

if (initialCandles && initialCandles.length > 0) {{
    series.setData(initialCandles);
    currentBar = {{ ...initialCandles[initialCandles.length - 1] }};
    const pEl = document.getElementById("price-txt");
    pEl.innerText = "$" + currentBar.close.toLocaleString("en-US", {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
}}

function renderActiveTrades() {{
    priceLines.forEach(l => {{ try {{ series.removePriceLine(l); }} catch(e){{}} }});
    priceLines = [];
    let tags = [];

    if (activeTrades && activeTrades.length > 0) {{
        activeTrades.forEach(t => {{
            const entry = Number(t.entry), sl = Number(t.sl), tp3 = Number(t.tp3);
            if (!Number.isFinite(entry)) return;
            tags.push("[" + (t.tf || "15M") + "] " + (t.dir || "") + " @ $" + entry.toFixed(0));
            priceLines.push(series.createPriceLine({{ price: entry, color: "#38bdf8", lineWidth: 2, title: "ENTRY" }}));
            if (Number.isFinite(sl)) priceLines.push(series.createPriceLine({{ price: sl, color: "#f43f5e", lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: "SL" }}));
            if (Number.isFinite(tp3)) priceLines.push(series.createPriceLine({{ price: tp3, color: "#10b981", lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: "TP3" }}));
        }});
    }}

    const tag = document.getElementById("active-tag");
    tag.innerText = tags.length > 0 ? "• ACTIVE: " + tags.join(" | ") : "SCANNING";
    tag.style.color = tags.length > 0 ? "#38bdf8" : "#64748b";
}}
renderActiveTrades();

// Direct Kline WebSocket
let wsKline = null;
function initKlineWS() {{
    wsKline = new WebSocket("wss://fstream.binance.com/ws/btcusdt@kline_15m");
    wsKline.onmessage = (e) => {{
        try {{
            const k = JSON.parse(e.data).k;
            currentBar = {{
                time: Math.floor(k.t / 1000),
                open: parseFloat(k.o),
                high: parseFloat(k.h),
                low: parseFloat(k.l),
                close: parseFloat(k.c)
            }};
            series.update(currentBar);
            const pEl = document.getElementById("price-txt");
            pEl.innerText = "$" + currentBar.close.toLocaleString("en-US", {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
            pEl.style.color = currentBar.close >= currentBar.open ? "#089981" : "#f23645";
        }} catch(err){{}}
    }};
    wsKline.onclose = () => {{ setTimeout(initKlineWS, 3000); }};
}}
initKlineWS();

// Micro Tick WebSocket
let wsTrade = null;
function initTradeWS() {{
    wsTrade = new WebSocket("wss://fstream.binance.com/ws/btcusdt@trade");
    wsTrade.onmessage = (e) => {{
        try {{
            const p = parseFloat(JSON.parse(e.data).p);
            if (currentBar && p > 10000) {{
                currentBar.close = p;
                if (p > currentBar.high) currentBar.high = p;
                if (p < currentBar.low) currentBar.low = p;
                series.update(currentBar);
                const pEl = document.getElementById("price-txt");
                pEl.innerText = "$" + p.toLocaleString("en-US", {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
                pEl.style.color = p >= currentBar.open ? "#089981" : "#f23645";
            }}
        }} catch(err){{}}
    }};
    wsTrade.onclose = () => {{ setTimeout(initTradeWS, 3000); }};
}}
initTradeWS();

function renderVault() {{
    let h = "";
    if (vault && vault.length > 0) {{
        vault.forEach(v => {{
            const pnl = Number(v.pnl || 0);
            h += `<div style="padding:7px 0; border-bottom:1px solid #161e2a;">
                <div style="display:flex; justify-content:space-between;">
                    <b>[${{v.tf || '15M'}}] ${{v.dir || ''}}</b> @ ${{Number(v.entry || 0).toFixed(1)}}
                    <span style="color:${{pnl >= 0 ? '#089981' : '#f43f5e'}}; font-weight:bold;">${{v.res || v.status || ''}} (${{pnl >= 0 ? '+' : ''}}${{pnl}}R)</span>
                </div>
                <div style="color:#64748b; font-size:9px; margin-top:2px;">Setup: ${{v.setup || 'SMC'}}</div>
            </div>`;
        }});
    }} else {{
        h = '<div style="color:#64748b; padding:10px 0; text-align:center;">No completed trades yet.</div>';
    }}
    document.getElementById("vaultContent").innerHTML = h;
}}

function toggleVault() {{
    const m = document.getElementById("vaultModal");
    m.style.display = m.style.display === "flex" ? "none" : "flex";
    if (m.style.display === "flex") renderVault();
}}

async function syncRender() {{
    try {{
        const res = await fetch("https://btc-live-terminal-2.onrender.com/vault-data", {{ cache: "no-store" }});
        if (res.ok) {{
            const data = await res.json();
            if (data.vault) {{
                vault = data.vault;
                document.getElementById("vault-count").innerText = vault.length;
            }}
            if (data.active) {{
                activeTrades = data.active;
                renderActiveTrades();
            }}
        }}
    }} catch(e){{}}
}}
setInterval(syncRender, 4000);

function handleResize() {{
    const w = container.clientWidth || window.innerWidth;
    const h = container.clientHeight || (window.innerHeight - 44);
    if (w > 0 && h > 0) {{
        chart.applyOptions({{ width: w, height: h }});
    }}
}}
window.addEventListener("resize", handleResize);
setTimeout(handleResize, 150);
setTimeout(handleResize, 600);
</script>
</body>
</html>
"""

components.html(HTML_PAYLOAD, height=920, scrolling=False)
