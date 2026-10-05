import os
import json
import requests
import streamlit as st
import streamlit.components.v1 as components

# ============================================================
# BTCUSDT SMC PRO DASHBOARD (COMPLETE FULL VERSION)
# LIVE 15M CANDLE + TICK + RENDER AUTO-SYNC + AUTO RECONNECT
# ============================================================

st.set_page_config(
    page_title="BTCUSDT SMC PRO",
    layout="wide",
    initial_sidebar_state="collapsed"
)

SYMBOL = "BTCUSDT"
RENDER_URL = "https://btc-live-terminal-2.onrender.com"

# ============================================================
# 1. FETCH STRUCTURED DATA FROM RENDER
# ============================================================
def get_render_sync():
    try:
        r = requests.get(f"{RENDER_URL}/vault-data", timeout=4)
        if r.status_code == 200:
            data = r.json()
            return data.get("vault", []), data.get("active", [])
    except Exception:
        pass
    return [], []

# ============================================================
# 2. FETCH INITIAL BINANCE FUTURES CANDLES
# ============================================================
def fetch_initial_candles(interval="15m", limit=200):
    urls = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
            raw = r.json()
            if isinstance(raw, list) and len(raw) > 20:
                return [
                    {
                        "time": int(b[0] // 1000),
                        "open": float(b[1]),
                        "high": float(b[2]),
                        "low": float(b[3]),
                        "close": float(b[4])
                    }
                    for b in raw
                ]
        except Exception:
            continue
    return []

# ============================================================
# 3. INITIAL DATA PREPARATION
# ============================================================
vault_list, active_list = get_render_sync()

total_trades = len(vault_list)
wins = sum(1 for r in vault_list if float(r.get("pnl", 0) or 0) > 0)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0
net_r = sum(float(r.get("pnl", 0) or 0) for r in vault_list)

candles = fetch_initial_candles("15m", 200)

vault_json = json.dumps(vault_list)
active_json = json.dumps(active_list)
candles_json = json.dumps(candles)

# ============================================================
# 4. FULLSCREEN CSS
# ============================================================
st.markdown(
    """
    <style>
    header, footer, #MainMenu {
        visibility: hidden !important;
        height: 0 !important;
    }
    .block-container {
        padding: 0 !important;
        margin: 0 !important;
        max-width: 100% !important;
        background-color: #06080d !important;
    }
    .stApp {
        background-color: #06080d !important;
    }
    iframe {
        border: none !important;
        width: 100vw !important;
        height: 100vh !important;
        display: block !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# ============================================================
# 5. LIVE TRADING TERMINAL HTML + JS
# ============================================================
ui_html = f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}}
body {{
    background: #06080d;
    color: #c3c7d1;
    overflow: hidden;
    width: 100vw;
    height: 100vh;
    display: flex;
    flex-direction: column;
}}
.nav {{
    height: 38px;
    min-height: 38px;
    background: #0c1017;
    border-bottom: 1px solid #161e2a;
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 10px;
    font-size: 11px;
}}
#chart-wrap {{
    flex: 1;
    width: 100vw;
    height: calc(100vh - 38px);
    position: relative;
    background: #06080d;
}}
.btn {{
    background: #16202f;
    border: 1px solid #233147;
    color: #38bdf8;
    padding: 4px 9px;
    border-radius: 4px;
    font-size: 10px;
    cursor: pointer;
    font-weight: bold;
}}
.btn:hover {{
    background: #202d42;
}}
.modal {{
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    background: rgba(0,0,0,0.85);
    z-index: 999;
    align-items: center;
    justify-content: center;
    padding: 12px;
}}
.box {{
    background: #0b0f16;
    border: 1px solid #1c2636;
    border-radius: 8px;
    width: 100%;
    max-width: 460px;
    padding: 14px;
    max-height: 85vh;
    overflow-y: auto;
}}
</style>
</head>
<body>

<div class="nav">
    <div style="display:flex; align-items:center; gap:6px;">
        <b style="color:#f59e0b;">BTCUSDT</b>
        <span style="background:rgba(8,153,129,0.2); color:#089981; font-size:8px; padding:1px 4px; border-radius:2px; font-weight:bold;">
            ● LIVE 15M
        </span>
        <span id="connection-status" style="color:#089981; font-size:9px; font-weight:bold;">
            BINANCE CONNECTING
        </span>
        <span id="active-tag" style="color:#38bdf8; font-size:9.5px; font-weight:600;">
            SCANNING
        </span>
    </div>

    <div style="display:flex; align-items:center; gap:8px;">
        <span id="price-txt" style="color:#089981; font-weight:bold;">CONNECTING...</span>
        <button class="btn" onclick="toggleVault()">
            📜 VAULT (<span id="vault-count">{total_trades}</span>)
        </button>
    </div>
</div>

<div id="chart-wrap"></div>

<div id="vaultModal" class="modal">
    <div class="box">
        <div style="display:flex; justify-content:space-between; margin-bottom:8px; border-bottom:1px solid #1c2636; padding-bottom:6px;">
            <b id="vault-title" style="color:#f8fafc; font-size:11px;">
                VAULT HISTORY ({total_trades} TRADES | NET: {net_r:+.1f}R)
            </b>
            <span style="cursor:pointer; color:#94a3b8;" onclick="toggleVault()">✕</span>
        </div>
        <div id="vaultContent" style="font-size:10px;"></div>
    </div>
</div>

<script>
let initialCandles = {candles_json};
let activeTrades = {active_json};
let vault = {vault_json};

const el = document.getElementById("chart-wrap");
const chart = LightweightCharts.createChart(el, {{
    layout: {{
        background: {{ type: "solid", color: "#06080d" }},
        textColor: "#787f8f",
        fontSize: 10
    }},
    grid: {{
        vertLines: {{ color: "rgba(255,255,255,0.02)" }},
        horzLines: {{ color: "rgba(255,255,255,0.02)" }}
    }},
    rightPriceScale: {{
        borderColor: "#161e2a",
        autoScale: true
    }},
    timeScale: {{
        borderColor: "#161e2a",
        timeVisible: true,
        secondsVisible: false,
        barSpacing: 8
    }}
}});

const series = chart.addCandlestickSeries({{
    upColor: "#089981",
    downColor: "#f23645",
    borderUpColor: "#089981",
    borderDownColor: "#f23645",
    wickUpColor: "#089981",
    wickDownColor: "#f23645"
}});

let currentBar = null;
let priceLines = [];

if (initialCandles && initialCandles.length > 0) {{
    series.setData(initialCandles);
    currentBar = {{ ...initialCandles[initialCandles.length - 1] }};
}}

function clearPriceLines() {{
    priceLines.forEach(line => {{
        try {{ series.removePriceLine(line); }} catch(e) {{}}
    }});
    priceLines = [];
}}

function renderActiveTrades() {{
    clearPriceLines();
    let tags = [];
    if (activeTrades && activeTrades.length > 0) {{
        activeTrades.forEach(t => {{
            const entry = Number(t.entry);
            const sl = Number(t.sl);
            const tp3 = Number(t.tp3);
            if (!Number.isFinite(entry) || !Number.isFinite(sl) || !Number.isFinite(tp3)) return;

            tags.push("[" + (t.tf || "15M") + "] " + (t.dir || "") + " @ $" + entry.toFixed(0));
            priceLines.push(series.createPriceLine({{ price: entry, color: "#38bdf8", lineWidth: 2, title: "ENTRY" }}));
            priceLines.push(series.createPriceLine({{ price: sl, color: "#f43f5e", lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: "SL" }}));
            priceLines.push(series.createPriceLine({{ price: tp3, color: "#10b981", lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: "TP3" }}));
        }});
    }}

    const tag = document.getElementById("active-tag");
    if (tags.length > 0) {{
        tag.innerText = "• ACTIVE: " + tags.join(" | ");
        tag.style.color = "#38bdf8";
    }} else {{
        tag.innerText = "SCANNING";
        tag.style.color = "#64748b";
    }}
}}

renderActiveTrades();

// =====================================================
// BINANCE 15M KLINE WEBSOCKET WITH AUTO-RECONNECT
// =====================================================
let klineWS = null;
let klineReconnectTimer = null;

function connectKlineWS() {{
    try {{
        if (klineWS) {{ try {{ klineWS.close(); }} catch(e) {{}} }}
        klineWS = new WebSocket("wss://fstream.binance.com/ws/btcusdt@kline_15m");

        klineWS.onopen = () => {{
            document.getElementById("connection-status").innerText = "● BINANCE LIVE";
            document.getElementById("connection-status").style.color = "#089981";
        }};

        klineWS.onmessage = (event) => {{
            try {{
                const data = JSON.parse(event.data);
                const k = data.k;
                const candleTime = Math.floor(k.t / 1000);
                const open = parseFloat(k.o);
                const high = parseFloat(k.h);
                const low = parseFloat(k.l);
                const close = parseFloat(k.c);

                currentBar = {{ time: candleTime, open: open, high: high, low: low, close: close }};
                series.update(currentBar);
                updatePrice(close, open);
            }} catch(err) {{
                console.error("KLINE ERROR:", err);
            }}
        }};

        klineWS.onclose = () => {{
            document.getElementById("connection-status").innerText = "RECONNECTING...";
            document.getElementById("connection-status").style.color = "#f59e0b";
            clearTimeout(klineReconnectTimer);
            klineReconnectTimer = setTimeout(connectKlineWS, 3000);
        }};

        klineWS.onerror = () => {{ try {{ klineWS.close(); }} catch(e) {{}} }};
    }} catch(e) {{
        setTimeout(connectKlineWS, 3000);
    }}
}}

// =====================================================
// LIVE TRADE WEBSOCKET FOR MICRO-TICKS
// =====================================================
let tradeWS = null;
let tradeReconnectTimer = null;

function connectTradeWS() {{
    try {{
        if (tradeWS) {{ try {{ tradeWS.close(); }} catch(e) {{}} }}
        tradeWS = new WebSocket("wss://fstream.binance.com/ws/btcusdt@trade");

        tradeWS.onmessage = (event) => {{
            try {{
                const t = JSON.parse(event.data);
                const p = parseFloat(t.p);
                if (currentBar && Number.isFinite(p) && p > 10000) {{
                    currentBar.close = p;
                    if (p > currentBar.high) currentBar.high = p;
                    if (p < currentBar.low) currentBar.low = p;
                    series.update(currentBar);

                    const priceEl = document.getElementById("price-txt");
                    priceEl.innerText = "$" + p.toLocaleString("en-US", {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
                    priceEl.style.color = p >= currentBar.open ? "#089981" : "#f23645";
                }}
            }} catch(err) {{}}
        }};

        tradeWS.onclose = () => {{
            clearTimeout(tradeReconnectTimer);
            tradeReconnectTimer = setTimeout(connectTradeWS, 3000);
        }};

        tradeWS.onerror = () => {{ try {{ tradeWS.close(); }} catch(e) {{}} }};
    }} catch(e) {{
        setTimeout(connectTradeWS, 3000);
    }}
}}

function updatePrice(price, open) {{
    const priceEl = document.getElementById("price-txt");
    priceEl.innerText = "$" + price.toLocaleString("en-US", {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
    priceEl.style.color = price >= open ? "#089981" : "#f23645";
}}

connectKlineWS();
connectTradeWS();

// =====================================================
// VAULT MODAL RENDER & TOGGLE
// =====================================================
function renderVault() {{
    const content = document.getElementById("vaultContent");
    let html = "";
    if (vault && vault.length > 0) {{
        vault.forEach(v => {{
            const pnl = Number(v.pnl || 0);
            const pnlColor = pnl >= 0 ? "#089981" : "#f43f5e";
            const sign = pnl >= 0 ? "+" : "";
            const entryStr = Number.isFinite(Number(v.entry)) ? Number(v.entry).toFixed(1) : "-";
            html += `<div style="padding:7px 0; border-bottom:1px solid #161e2a;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <b>[${{v.tf || '15M'}}] ${{v.dir || ''}}</b> @ ${{entryStr}}
                    <span style="color:${{pnlColor}}; font-weight:bold;">${{v.res || v.status || ''}} (${{sign}}${{pnl}}R)</span>
                </div>
                <div style="color:#64748b; font-size:9px; margin-top:2px;">Setup: ${{v.setup || 'SMC'}} | Status: <b>${{v.status || ''}}</b></div>
            </div>`;
        }});
    }} else {{
        html = '<div style="color:#64748b; padding:12px 0; text-align:center;">No trades recorded in vault yet.</div>';
    }}
    content.innerHTML = html;
}}

function toggleVault() {{
    const m = document.getElementById("vaultModal");
    m.style.display = m.style.display === "flex" ? "none" : "flex";
    if (m.style.display === "flex") {{
        renderVault();
    }}
}}

// =====================================================
// BACKGROUND RENDER SYNC POLLING (EVERY 3 SECONDS)
// =====================================================
async function pollRenderSync() {{
    try {{
        const res = await fetch("{RENDER_URL}/vault-data", {{ cache: "no-store" }});
        if (res.ok) {{
            const data = await res.json();
            if (data.vault) {{
                vault = data.vault;
                document.getElementById("vault-count").innerText = vault.length;
                let net = 0;
                vault.forEach(x => {{ net += Number(x.pnl || 0); }});
                document.getElementById("vault-title").innerText = `VAULT HISTORY (${{vault.length}} TRADES | NET: ${{net >= 0 ? '+' : ''}}${{net.toFixed(1)}}R)`;
            }}
            if (data.active) {{
                activeTrades = data.active;
                renderActiveTrades();
            }}
        }}
    }} catch(e) {{}}
}}

setInterval(pollRenderSync, 3000);
window.addEventListener("resize", () => chart.applyOptions({{ width: el.clientWidth, height: el.clientHeight }}));
</script>
</body>
</html>
"""

components.html(ui_html, height=880, scrolling=False)
