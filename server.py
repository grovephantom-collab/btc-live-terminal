import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="BTCUSDT PRO TERMINAL",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Streamlit default padding wipeout
st.markdown("""
<style>
    header, footer, #MainMenu { display: none !important; }
    .block-container { padding: 0px !important; margin: 0px !important; max-width: 100% !important; }
    .stApp { background-color: #06080d !important; }
    iframe { width: 100% !important; min-height: 650px !important; border: none !important; }
</style>
""", unsafe_allow_html=True)

# Complete Self-Contained Mobile Terminal (Zero Crash / Zero 0px Blank Screen)
TERMINAL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>BTCUSDT Terminal</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    html, body { width: 100%; height: 100%; background: #06080d; color: #c3c7d1; overflow-x: hidden; }
    
    /* Top Header Bar */
    .header {
        height: 48px;
        background: #0c1017;
        border-bottom: 1px solid #161e2a;
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0 10px;
    }
    .badge {
        background: rgba(8, 153, 129, 0.2);
        color: #089981;
        font-size: 10px;
        font-weight: 700;
        padding: 2px 6px;
        border-radius: 4px;
    }
    .signal-status {
        color: #38bdf8;
        font-size: 10px;
        font-weight: 600;
        margin-left: 6px;
    }
    .price-badge {
        font-size: 14px;
        font-weight: 800;
        color: #089981;
    }
    .vault-btn {
        background: #16202f;
        border: 1px solid #233147;
        color: #38bdf8;
        font-size: 11px;
        font-weight: 700;
        padding: 5px 9px;
        border-radius: 4px;
        cursor: pointer;
    }

    /* Timeframe Selector */
    .tf-bar {
        height: 36px;
        background: #080c12;
        border-bottom: 1px solid #161e2a;
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 0 10px;
        overflow-x: auto;
    }
    .tf-btn {
        background: #111823;
        border: 1px solid #1c2636;
        color: #787f8f;
        font-size: 10px;
        font-weight: 700;
        padding: 3px 8px;
        border-radius: 4px;
        cursor: pointer;
    }
    .tf-btn.active {
        background: #38bdf8;
        color: #06080d;
        border-color: #38bdf8;
    }

    /* Fixed Height Chart Frame (Cannot Collapse to 0px) */
    #chart-box {
        width: 100%;
        height: 520px;
        min-height: 520px;
        position: relative;
        background: #06080d;
    }

    /* Vault Modal */
    .modal {
        display: none;
        position: fixed;
        top: 0; left: 0;
        width: 100vw; height: 100vh;
        background: rgba(0,0,0,0.85);
        z-index: 9999;
        align-items: center;
        justify-content: center;
        padding: 15px;
    }
    .modal-box {
        background: #0b0f16;
        border: 1px solid #1c2636;
        border-radius: 8px;
        width: 100%;
        max-width: 440px;
        max-height: 80vh;
        padding: 14px;
        overflow-y: auto;
    }
</style>
</head>
<body>

<!-- Navigation Header -->
<div class="header">
    <div style="display:flex; align-items:center;">
        <span style="color:#f59e0b; font-weight:800; font-size:13px; margin-right:6px;">BTCUSDT</span>
        <span class="badge">● FUTURES</span>
        <span id="active-tag" class="signal-status">ACTIVE: SCANNING</span>
    </div>
    <div style="display:flex; align-items:center; gap:8px;">
        <span id="live-price" class="price-badge">$---.--</span>
        <button class="vault-btn" onclick="openVault()">📜 VAULT (<span id="vault-count">0</span>)</button>
    </div>
</div>

<!-- Timeframe Buttons -->
<div class="tf-bar">
    <button class="tf-btn" onclick="setTF('1')">1M</button>
    <button class="tf-btn" onclick="setTF('5')">5M</button>
    <button class="tf-btn active" onclick="setTF('15')">15M</button>
    <button class="tf-btn" onclick="setTF('60')">1H</button>
    <button class="tf-btn" onclick="setTF('240')">4H</button>
    <button class="tf-btn" onclick="setTF('D')">1D</button>
</div>

<!-- Live Chart Frame (Self Sustained) -->
<div id="chart-box">
    <div id="tv-widget-container" style="width:100%; height:100%;"></div>
</div>

<!-- Vault Modal -->
<div id="vaultModal" class="modal">
    <div class="modal-box">
        <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1c2636; padding-bottom:8px; margin-bottom:10px;">
            <b style="color:#f8fafc; font-size:12px;">SIGNAL & VAULT HISTORY</b>
            <span style="color:#94a3b8; font-size:16px; cursor:pointer;" onclick="closeVault()">✕</span>
        </div>
        <div id="vault-items" style="font-size:11px;">
            <div style="color:#64748b; text-align:center; padding:15px;">Scanning for SMC entries...</div>
        </div>
    </div>
</div>

<!-- TradingView Native CDN (100% Reliable across all mobile devices) -->
<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script type="text/javascript">
    let tvWidget = null;
    let currentInterval = "15";

    // 1. Initialize Safe TradingView Chart Widget
    function loadChart(interval) {
        currentInterval = interval;
        const container = document.getElementById("tv-widget-container");
        container.innerHTML = "";

        tvWidget = new TradingView.widget({
            "autosize": true,
            "symbol": "BINANCE:BTCUSDT.P",
            "interval": interval,
            "timezone": "Asia/Kolkata",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "toolbar_bg": "#06080d",
            "enable_publishing": false,
            "hide_top_toolbar": true,
            "hide_legend": false,
            "save_image": false,
            "container_id": "tv-widget-container",
            "backgroundColor": "#06080d",
            "gridColor": "rgba(255, 255, 255, 0.03)",
            "disabled_features": [
                "use_localstorage_for_settings",
                "header_widget",
                "left_toolbar",
                "control_bar",
                "timeframes_toolbar"
            ]
        });
    }

    loadChart("15");

    function setTF(tf) {
        document.querySelectorAll(".tf-btn").forEach(btn => btn.classList.remove("active"));
        event.target.classList.add("active");
        loadChart(tf);
    }

    // 2. Direct Binance Futures Sub-Second Price Stream
    const ws = new WebSocket("wss://fstream.binance.com/ws/btcusdt@trade");
    const pEl = document.getElementById("live-price");
    let lastP = 0;

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            const p = parseFloat(data.p);
            if (p > 1000) {
                pEl.innerText = "$" + p.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
                pEl.style.color = p >= lastP ? "#089981" : "#f43f5e";
                lastP = p;
            }
        } catch(e) {}
    };

    ws.onclose = () => {
        setTimeout(() => { location.reload(); }, 5000);
    };

    // 3. Render 4-Second Auto-Sync with Signals & Vault
    async function syncBackend() {
        try {
            const res = await fetch("https://btc-live-terminal-2.onrender.com/vault-data", { cache: "no-store" });
            if (!res.ok) return;
            const data = await res.json();

            // Active Trade Badge Update
            const activeTag = document.getElementById("active-tag");
            if (data.active && data.active.length > 0) {
                const t = data.active[0];
                activeTag.innerText = "ACTIVE: [" + (t.type || "TRADE") + "] " + t.dir + " @ $" + Number(t.entry).toFixed(0);
                activeTag.style.color = "#38bdf8";
            } else {
                activeTag.innerText = "ACTIVE: SCANNING";
                activeTag.style.color = "#64748b";
            }

            // Vault Counter & List Update
            if (data.vault) {
                document.getElementById("vault-count").innerText = data.vault.length;
                let html = "";
                if (data.vault.length === 0) {
                    html = '<div style="color:#64748b; text-align:center; padding:15px;">No completed trades yet.</div>';
                } else {
                    data.vault.forEach(v => {
                        const pnl = Number(v.pnl || 0);
                        html += `
                            <div style="padding:8px 0; border-bottom:1px solid #161e2a;">
                                <div style="display:flex; justify-content:space-between;">
                                    <b style="color:#f8fafc;">[${v.tf || '15M'}] ${v.dir || ''} @ $${Number(v.entry || 0).toFixed(1)}</b>
                                    <span style="color:${pnl >= 0 ? '#089981' : '#f43f5e'}; font-weight:800;">
                                        ${v.res || v.status} (${pnl >= 0 ? '+' : ''}${pnl}R)
                                    </span>
                                </div>
                                <div style="color:#64748b; font-size:10px; margin-top:2px;">Setup: ${v.setup || 'SMC'}</div>
                            </div>
                        `;
                    });
                }
                document.getElementById("vault-items").innerHTML = html;
            }
        } catch(e) {}
    }

    setInterval(syncBackend, 4000);
    syncBackend();

    function openVault() { document.getElementById("vaultModal").style.display = "flex"; }
    function closeVault() { document.getElementById("vaultModal").style.display = "none"; }
</script>
</body>
</html>
"""

components.html(TERMINAL_HTML, height=650, scrolling=False)
