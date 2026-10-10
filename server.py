#!/usr/bin/env python3
"""
TIER 2: INSTITUTIONAL FRONTEND DASHBOARD (server.py - Streamlit)
Fully compatible with live Render worker /vault-data pipeline.
"""

import os
import time
from typing import Dict, Any, Optional

import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# ==========================================
# CONFIGURATION
# ==========================================
BACKEND_URL = os.environ.get("BACKEND_URL", "https://btc-live-terminal-3.onrender.com").rstrip("/")
VAULT_ENDPOINT = f"{BACKEND_URL}/vault-data"
AUTO_REFRESH_SECONDS = int(os.environ.get("AUTO_REFRESH_SECONDS", "5"))

st.set_page_config(
    page_title="SMC Institutional BTC/USDT Vault & Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ==========================================
# INSTITUTIONAL STYLING
# ==========================================
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1.25rem;
            padding-bottom: 2rem;
            max-width: 100%;
        }
        .metric-card {
            background-color: #0f172a;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 14px 18px;
            font-variant-numeric: tabular-nums;
        }
        .metric-label {
            color: #94a3b8;
            font-size: 0.78rem;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            margin-bottom: 4px;
        }
        .metric-value {
            color: #f8fafc;
            font-size: 1.45rem;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }
        .metric-sub {
            color: #64748b;
            font-size: 0.75rem;
            margin-top: 4px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def fetch_vault_payload() -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(VAULT_ENDPOINT, timeout=6)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"Unable to reach Scanner Engine at `{VAULT_ENDPOINT}`: {exc}")
        return None


def render_chart_with_overlay(active_trade: Optional[Dict[str, Any]], spot_price: float) -> None:
    if active_trade:
        direction = active_trade.get("direction", "LONG")
        setup_type = active_trade.get("signal_type") or active_trade.get("setup", "SMC SETUP")
        entry = float(active_trade.get("entry", 0.0))
        sl = float(active_trade.get("sl", 0.0))
        tp1 = float(active_trade.get("tp1", 0.0))
        tp2 = float(active_trade.get("tp2", 0.0))
        risk_usd = round(abs(entry - sl), 2)
        is_risk_free = active_trade.get("status") == "RISK_FREE"

        dir_color = "#10b981" if direction == "LONG" else "#ef4444"
        be_badge = "🛡️ RISK-FREE (SL @ ENTRY)" if is_risk_free else "⚡ LIVE RISK ACTIVE"

        overlay_html = f"""
        <div style="
            position: absolute;
            top: 16px;
            right: 20px;
            z-index: 50;
            width: 310px;
            background: rgba(15, 23, 42, 0.92);
            backdrop-filter: blur(8px);
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 14px 16px;
            color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
        ">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <span style="font-size:12px; font-weight:700; color:{dir_color}; letter-spacing:0.05em;">
                    ● ACTIVE {direction} · {setup_type}
                </span>
            </div>
            <div style="font-size:11px; color:#94a3b8; margin-bottom:10px; border-bottom:1px solid #1e293b; padding-bottom:6px;">
                {be_badge}
            </div>
            <div style="display:grid; grid-template-columns: 1fr 1fr; row-gap: 8px; column-gap: 12px; font-size:12px;">
                <div>
                    <div style="color:#64748b; font-size:10px;">ENTRY PRICE</div>
                    <div style="font-weight:700; color:#f8fafc;">${entry:,.2f}</div>
                </div>
                <div>
                    <div style="color:#64748b; font-size:10px;">STOP LOSS (SL)</div>
                    <div style="font-weight:700; color:#ef4444;">${sl:,.2f}</div>
                </div>
                <div>
                    <div style="color:#64748b; font-size:10px;">TARGET 1 (2.0R)</div>
                    <div style="font-weight:700; color:#38bdf8;">${tp1:,.2f}</div>
                </div>
                <div>
                    <div style="color:#64748b; font-size:10px;">TARGET 2 (3.5R)</div>
                    <div style="font-weight:700; color:#10b981;">${tp2:,.2f}</div>
                </div>
            </div>
            <div style="margin-top:10px; padding-top:8px; border-top:1px solid #1e293b; display:flex; justify-content:space-between; font-size:11px; color:#94a3b8;">
                <span>Risk Envelope: <b>${risk_usd:,.2f}</b></span>
                <span>Spot: <b>${spot_price:,.2f}</b></span>
            </div>
        </div>
        """
    else:
        overlay_html = """
        <div style="
            position: absolute;
            top: 16px;
            right: 20px;
            z-index: 50;
            background: rgba(15, 23, 42, 0.88);
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 10px 14px;
            color: #94a3b8;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
            font-size: 12px;
        ">
            <span style="color:#38bdf8; font-weight:600;">SCANNING 15M STRUCTURE</span> · No Active Position
        </div>
        """

    html_code = f"""
    <div style="position: relative; width: 100%; height: 600px; border: 1px solid #1e293b; border-radius: 8px; overflow: hidden; background: #090d16;">
        {overlay_html}
        <div id="tv_chart_container" style="width: 100%; height: 100%;"></div>
        <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
        <script type="text/javascript">
        new TradingView.widget({{
            "autosize": true,
            "symbol": "BINANCE:BTCUSDT",
            "interval": "15",
            "timezone": "Etc/UTC",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "enable_publishing": false,
            "backgroundColor": "rgba(9, 13, 22, 1)",
            "gridColor": "rgba(30, 41, 59, 0.35)",
            "hide_top_toolbar": false,
            "hide_legend": false,
            "save_image": false,
            "container_id": "tv_chart_container"
        }});
        </script>
    </div>
    """
    components.html(html_code, height=615)


def main() -> None:
    head_left, head_right = st.columns([3, 1])
    with head_left:
        st.subheader("Institutional BTC/USDT SMC Terminal")
        st.caption("Live Coinbase Stream · Gautam Jha Liquidity Model · Saurabh Jha Risk Engine")
    with head_right:
        live_sync = st.toggle("Live Auto-Sync (5s)", value=True)

    payload = fetch_vault_payload()
    if not payload:
        st.stop()

    # Data Hub Safe Extraction (Matches Live worker.py)
    spot_price = float(payload.get("price") or 0.0)
    pdh = float(payload.get("pdh") or 0.0)
    pdl = float(payload.get("pdl") or 0.0)
    active_session = payload.get("session", "ACTIVE")
    stats = payload.get("stats", {})

    active_list = payload.get("active", [])
    active_trade = active_list[0] if active_list else None
    vault_trades = payload.get("vault", [])
    pois = payload.get("pois", [])

    bsl_val = next((p["price"] for p in pois if p.get("type") == "BSL"), 0.0)
    ssl_val = next((p["price"] for p in pois if p.get("type") == "SSL"), 0.0)

    # 1. LIVE METRICS BAR
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-label">BTC-USD Spot Price</div>
                <div class="metric-value">${spot_price:,.2f}</div>
                <div class="metric-sub">Session: {active_session}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-label">Previous Day High (PDH)</div>
                <div class="metric-value">${pdh:,.2f}</div>
                <div class="metric-sub">Swing High (BSL): ${bsl_val:,.2f}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-label">Previous Day Low (PDL)</div>
                <div class="metric-value">${pdl:,.2f}</div>
                <div class="metric-sub">Swing Low (SSL): ${ssl_val:,.2f}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c4:
        total_r = stats.get("total_r", 0.0)
        win_rate = stats.get("win_rate", 0.0)
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-label">Vault Performance</div>
                <div class="metric-value">{total_r:+.2f}R</div>
                <div class="metric-sub">Win Rate: {win_rate}% ({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.write("")

    # 2. TRADINGVIEW CHART WITH OVERLAY
    render_chart_with_overlay(active_trade, spot_price)

    st.write("")
    st.markdown("### Institutional Trade Vault")

    # 3. HISTORICAL VAULT TABLE
    if vault_trades:
        df = pd.DataFrame(vault_trades)
        formatted_rows = []
        for _, row in df.iterrows():
            exit_p = row.get("exit")
            pnl_r = row.get("pnl_r")
            pnl_pct = row.get("pnl_percent")
            formatted_rows.append(
                {
                    "Date": row.get("created_date", ""),
                    "Time": row.get("created_at", ""),
                    "Direction": row.get("direction", ""),
                    "Setup": row.get("signal_type", ""),
                    "Entry": f"${float(row.get('entry', 0.0)):,.2f}",
                    "SL": f"${float(row.get('sl', 0.0)):,.2f}",
                    "TP1": f"${float(row.get('tp1', 0.0)):,.2f}",
                    "TP2": f"${float(row.get('tp2', 0.0)):,.2f}",
                    "Exit": f"${float(exit_p):,.2f}" if pd.notnull(exit_p) and exit_p else "—",
                    "Outcome": row.get("result") or "ACTIVE",
                    "PnL (R)": f"{float(pnl_r):+.2f}R" if pd.notnull(pnl_r) and pnl_r is not None else "0.00R",
                    "PnL (%)": f"{float(pnl_pct):+.2f}%" if pd.notnull(pnl_pct) and pnl_pct is not None else "0.00%",
                    "Status": row.get("status", "OPEN"),
                }
            )
        vault_df = pd.DataFrame(formatted_rows)
        st.dataframe(vault_df, use_container_width=True, hide_index=True)
    else:
        st.info("No trades in vault yet. Scanner is monitoring for setups.")

    if live_sync:
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()


if __name__ == "__main__":
    main()
