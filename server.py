import os, requests, streamlit as st, pandas as pd

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:10000").rstrip("/")
VAULT_ENDPOINT = f"{BACKEND_URL}/vault-data"

st.set_page_config(page_title="Delta SMC Terminal", page_icon="⚡", layout="wide")

st.subheader("⚡ Delta Exchange SMC Institutional Terminal")
try:
    resp = requests.get(VAULT_ENDPOINT, timeout=6)
    data = resp.json()
    st.metric("BTC Spot Price", f"${data.get('price', 0):,.2f}", f"Mode: {data.get('mode', 'PAPER')}")
    st.metric("Realized PnL", f"${data.get('realized_pnl_usd', 0):+.2f}")
    
    vault = data.get("vault", [])
    if vault:
        st.dataframe(pd.DataFrame(vault), use_container_width=True)
    else:
        st.info("Scanning for setups...")
except Exception as e:
    st.error(f"Connecting to backend failed: {e}")
