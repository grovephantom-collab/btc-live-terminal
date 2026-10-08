import streamlit as st
import pandas as pd
import requests
import streamlit.components.v1 as components

st.set_page_config(page_title="Gautam Jha Institutional Terminal", layout="wide", page_icon="👑")

BACKEND_URL = "https://btc-live-terminal-3.onrender.com/vault-data"

@st.cache_data(ttl=5)
def get_terminal_data():
    try:
        r = requests.get(BACKEND_URL, timeout=4).json()
        return r
    except Exception:
        return {"vault": [], "active": [], "pois": [], "price": 0.0, "pdh": 0.0, "pdl": 0.0, "session": "--"}

data = get_terminal_data()

st.title("👑 Institutional Price Action Terminal (BTCUSDT)")

# Top KPI Metrics Cards
c1, c2, c3, c4 = st.columns(4)
c1.metric("Live Price", f"${data.get('price', 0):,.2f}")
c2.metric("PDH Level", f"${data.get('pdh', 0):,.2f}")
c3.metric("PDL Level", f"${data.get('pdl', 0):,.2f}")
c4.metric("Session", data.get("session", "SYNCING"))

# Active Trades Info & Chart Overlays
active_trades = data.get("active", [])
active_overlays = ""

if active_trades:
    t = active_trades[0]
    st.info(f"🚨 **ACTIVE TRADE TRIGGERED:** {t['dir']} | Entry: ${t['entry']:,.2f} | SL: ${t['sl']:,.2f} | TP1: ${t['tp1']:,.2f} | TP2: ${t['tp2']:,.2f}")
    
    # Chart screen par Entry, SL aur TP levels ka live box
    active_overlays = f"""
    <div style="position: absolute; top: 15px; left: 15px; background: rgba(15, 23, 42, 0.88); padding: 12px 18px; border-radius: 8px; border: 1px solid #38bdf8; z-index: 1000; font-family: monospace; font-size: 13px; color: white; box-shadow: 0 4px 12px rgba(0,0,0,0.5);">
        <span style="color: #38bdf8; font-weight: bold;">🔵 ENTRY: ${t['entry']:,.2f}</span><br>
        <span style="color: #f87171; font-weight: bold;">🔴 STOP LOSS: ${t['sl']:,.2f}</span><br>
        <span style="color: #4ade80; font-weight: bold;">🟢 TARGET 1 (1:2): ${t['tp1']:,.2f}</span><br>
        <span style="color: #22c55e; font-weight: bold;">🔥 TARGET 2 (1:3.5): ${t['tp2']:,.2f}</span>
    </div>
    """

# Live 15M Institutional Chart
st.subheader("📊 Live Institutional 15M Price Action Chart")
chart_html = f"""
<div style="position: relative; width: 100%; height: 530px;">
    {active_overlays}
    <!-- TradingView Widget BEGIN -->
    <div class="tradingview-widget-container" style="height:100%;width:100%">
      <div id="tradingview_chart" style="height:calc(100% - 32px);width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget(
      {{
        "autosize": true,
        "symbol": "COINBASE:BTCUSD",
        "interval": "15",
        "timezone": "Etc/UTC",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "toolbar_bg": "#0f172a",
        "enable_publishing": false,
        "hide_side_toolbar": false,
        "allow_symbol_change": false,
        "container_id": "tradingview_chart"
      }}
      );
      </script>
    </div>
    <!-- TradingView Widget END -->
</div>
"""
components.html(chart_html, height=550)

# Complete Trade Vault History Table
st.subheader("🛡️ Trade Vault (All Completed & Open Orders)")
vault_data = data.get("vault", [])

if vault_data:
    df = pd.DataFrame(vault_data)
    # Column mapping & structure
    df = df[["created_at", "closed_at", "dir", "type", "entry", "sl", "tp1", "exit", "res", "pnl_r", "pnl_percent", "status"]]
    df.columns = ["Entry Time", "Exit Time", "Direction", "Pattern Type", "Entry ($)", "SL ($)", "TP1 ($)", "Exit ($)", "Result", "PnL (R)", "PnL (%)", "Status"]
    st.dataframe(df, use_container_width=True)
else:
    st.write("No trades logged yet. Awaiting fresh institutional liquidity trap.")
