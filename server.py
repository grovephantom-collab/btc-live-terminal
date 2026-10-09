import streamlit as st
import pandas as pd
import requests
import streamlit.components.v1 as components

st.set_page_config(page_title="Gautam Jha Terminal", layout="wide", page_icon="👑")

st.markdown("""
<style>
    .block-container { padding: 1rem; }
    h1 { font-size: 1.6rem !important; }
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=3)
def get_data():
    try: return requests.get("https://btc-live-terminal-3.onrender.com/vault-data", timeout=4).json()
    except: return {"vault": [], "active": [], "price": 0, "pdh": 0, "pdl": 0, "session": "--"}

data = get_data()

st.title("👑 Institutional Price Action Terminal")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Live Price", f"${data.get('price', 0):,.2f}")
c2.metric("PDH Level", f"${data.get('pdh', 0):,.2f}")
c3.metric("PDL Level", f"${data.get('pdl', 0):,.2f}")
c4.metric("Market Session", data.get("session", "SYNCING"))

# CHART & LIVE OVERLAY
st.subheader("📊 Live 15M Chart & Entry Levels")

overlay_html = ""
active = data.get("active", [])
if active:
    t = active[0]
    st.success(f"🚨 **ACTIVE {t['dir']} TRADE** | 🧠 Logic: {t['setup']}")
    overlay_html = f"""
    <div style="position: absolute; top: 10px; left: 10px; background: rgba(15,23,42,0.9); padding: 12px; border: 1px solid #38bdf8; border-radius: 8px; z-index: 1000; color: white; font-family: monospace; font-size: 14px;">
        <div style="color: #38bdf8; margin-bottom: 4px;">🔵 ENTRY: ${t['entry']:,.2f}</div>
        <div style="color: #f87171; margin-bottom: 4px;">🔴 SL: ${t['sl']:,.2f}</div>
        <div style="color: #4ade80; margin-bottom: 4px;">🟢 TP1 (1:2): ${t['tp1']:,.2f}</div>
        <div style="color: #22c55e;">🔥 TP2 (1:3.5): ${t['tp2']:,.2f}</div>
    </div>
    """

chart_container = f"""
<div style="position: relative; height: 550px; width: 100%;">
    {overlay_html}
    <iframe src="https://s.tradingview.com/widgetembed/?symbol=BINANCE%3ABTCUSDT&interval=15&theme=dark&style=1&timezone=Asia%2FKolkata&locale=en" 
            width="100%" height="100%" frameborder="0" allowtransparency="true" scrolling="no"></iframe>
</div>
"""
components.html(chart_container, height=570)

# TRADE VAULT TABLE
st.subheader("🛡️ Trade Vault (Full History & Percentage)")
vault = data.get("vault", [])
if vault:
    df = pd.DataFrame(vault)
    cols = ["created_at", "closed_at", "dir", "type", "entry", "sl", "tp1", "exit", "res", "pnl_r", "pnl_percent", "status"]
    df = df[[c for c in cols if c in df.columns]]
    df.columns = ["Entry Time", "Exit Time", "Dir", "Pattern", "Entry $", "SL $", "TP1 $", "Exit $", "Result", "PnL (R)", "PnL (%)", "Status"]
    st.dataframe(df, use_container_width=True)
else:
    st.info("System Ready. High-probability liquidity setups will log here automatically.")
