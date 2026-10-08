import streamlit as st
import pandas as pd
import requests
import streamlit.components.v1 as components

st.set_page_config(page_title="Gautam Jha Institutional Terminal", layout="wide", page_icon="👑")

# Mobile aur desktop par clean full width styling
st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 1rem; padding-left: 1rem; padding-right: 1rem; }
    h1 { font-size: 1.6rem !important; }
</style>
""", unsafe_allow_html=True)

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

# Top KPI Metric Cards
c1, c2, c3, c4 = st.columns(4)
c1.metric("Live Price", f"${data.get('price', 0):,.2f}")
c2.metric("PDH Level", f"${data.get('pdh', 0):,.2f}")
c3.metric("PDL Level", f"${data.get('pdl', 0):,.2f}")
c4.metric("Session", data.get("session", "SYNCING"))

# Active Trade Banner
active_trades = data.get("active", [])
if active_trades:
    t = active_trades[0]
    st.success(
        f"🚨 **LIVE ORDER ACTIVE ({t['dir']}):** Entry: `${t['entry']:,.2f}` | "
        f"🛑 SL: `${t['sl']:,.2f}` | 🎯 TP1: `${t['tp1']:,.2f}` | 🔥 TP2: `${t['tp2']:,.2f}`"
    )

st.subheader("📊 Live Institutional 15M Price Action Chart")

# Original TradingView Embed - 100% Mobile & Desktop Responsive
tradingview_widget = """
<!-- TradingView Widget BEGIN -->
<div class="tradingview-widget-container" style="height:620px; width:100%;">
  <div id="tradingview_full_chart" style="height:calc(100% - 32px);width:100%"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
  <script type="text/javascript">
  new TradingView.widget(
  {
    "autosize": true,
    "symbol": "BINANCE:BTCUSDT",
    "interval": "15",
    "timezone": "Asia/Kolkata",
    "theme": "dark",
    "style": "1",
    "locale": "en",
    "toolbar_bg": "#131722",
    "enable_publishing": false,
    "hide_top_toolbar": false,
    "allow_symbol_change": false,
    "save_image": false,
    "container_id": "tradingview_full_chart"
  }
  );
  </script>
</div>
<!-- TradingView Widget END -->
"""
components.html(tradingview_widget, height=640)

# Trade Vault History Table
st.subheader("🛡️ Trade Vault (All Completed & Open Orders)")
vault_data = data.get("vault", [])

if vault_data:
    df = pd.DataFrame(vault_data)
    df = df[["created_at", "closed_at", "dir", "type", "entry", "sl", "tp1", "exit", "res", "pnl_r", "pnl_percent", "status"]]
    df.columns = ["Entry Time", "Exit Time", "Direction", "Pattern Type", "Entry ($)", "SL ($)", "TP1 ($)", "Exit ($)", "Result", "PnL (R)", "PnL (%)", "Status"]
    st.dataframe(df, use_container_width=True)
else:
    st.write("No trades logged yet. Awaiting fresh institutional liquidity trap.")
