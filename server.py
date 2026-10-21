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
BACKEND_URL = os.getenv("BACKEND_URL", "https://btc-live-terminal.onrender.com").rstrip("/")
VAULT_ENDPOINT = f"{BACKEND_URL}/vault-data"
AUTO_REFRESH_SECONDS = int(os.getenv("AUTO_REFRESH_SECONDS", "5"))

st.set_page_config(
    page_title="SMC Institutional BTC/USDT Vault & Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ==========================================
# INSTITUTIONAL STYLING (DARK TERMINAL THEME)
# ==========================================
st.markdown(
    """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600;700&display=swap');
        
        html, body, [class*="css"] {
            font-family: 'Inter', sans-serif;
            background-color: #07090e;
            color: #f8fafc;
        }
        .block-container {
            padding-top: 1rem;
            padding-bottom: 2rem;
            max-width: 100%;
        }
        .metric-card {
            background: linear-gradient(135deg, #0f172a 0%, #090d16 100%);
            border: 1px solid #1e293b;
            border-radius: 10px;
            padding: 16px 20px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
            position: relative;
            overflow: hidden;
        }
        .metric-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; width: 3px; height: 100%;
            background: #38bdf8;
        }
        .metric-label {
            color: #94a3b8;
            font-size: 0.75rem;
            font-weight: 600;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            margin-bottom: 6px;
        }
        .metric-value {
            color: #f8fafc;
            font-size: 1.6rem;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
            letter-spacing: -0.02em;
        }
        .metric-sub {
            color: #64748b;
            font-size: 0.75rem;
            margin-top: 6px;
            font-family: 'JetBrains Mono', monospace;
        }
        .stAlert {
            background-color: #0f172a;
            color: #f8fafc;
            border: 1px solid #1e293b;
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
        st.error(f"⚠️ Unable to reach Institutional Scanner Engine at `{VAULT_ENDPOINT}`: {exc}")
        return None


def render_chart_with_overlay(active_trade: Optional[Dict[str, Any]], spot_price: float) -> None:
    if active_trade:
        direction = active_trade.get("direction", "LONG")
        setup_type = active_trade.get("setup_name") or active_trade.get("setup", "SMC SETUP")
        entry = float(active_trade.get("entry_price", active_trade.get("entry", 0.0)))
        sl = float(active_trade.get("stop_price", active_trade.get("sl", 0.0)))
        tp1 = float(active_trade.get("tp1_price", active_trade.get("tp1", 0.0)))
        tp2 = float(active_trade.get("tp2_price", active_trade.get("tp2", 0.0)))
        risk_usd = round(abs(entry - sl), 2)
        is_risk_free = active_trade.get("status") == "RISK_FREE" or active_trade.get("tp1_done") == 1

        dir_color = "#10b981" if direction == "LONG" else "#ef4444"
        be_badge = "🛡️ RISK-FREE (SL @ ENTRY)" if is_risk_free else "⚡ LIVE RISK ACTIVE ($5 FLOOR)"

        overlay_html = f"""
        <div style="
            position: absolute;
            top: 16px;
            right: 20px;
            z-index: 50;
            width: 320px;
            background: rgba(15, 23, 42, 0.94);
            backdrop-filter: blur(12px);
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 16px;
            color: #f8fafc;
            font-family: 'JetBrains Mono', monospace;
            box-shadow: 0 12px 30px rgba(0, 0, 0, 0.6);
        ">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <span style="font-size:11px; font-weight:700; color:{dir_color}; letter-spacing:0.06em;">
                    ● ACTIVE {direction} · {setup_type}
                </span>
            </div>
            <div style="font-size:10px; color:#94a3b8; margin-bottom:10px; border-bottom:1px solid #1e293b; padding-bottom:6px;">
                {be_badge}
            </div>
            <div style="display:grid; grid-template-columns: 1fr 1fr; row-gap: 10px; column-gap: 12px; font-size:11px;">
                <div>
                    <div style="color:#64748b; font-size:9px;">ENTRY PRICE</div>
                    <div style="font-weight:700; color:#f8fafc;">${entry:,.2f}</div>
                </div>
                <div>
                    <div style="color:#64748b; font-size:9px;">STOP LOSS</div>
                    <div style="font-weight:700; color:#ef4444;">${sl:,.2f}</div>
                </div>
                <div>
                    <div style="color:#64748b; font-size:9px;">TARGET 1 (1.5R)</div>
                    <div style="font-weight:700; color:#38bdf8;">${tp1:,.2f}</div>
                </div>
                <div>
                    <div style="color:#64748b; font-size:9px;">TARGET 2 (2.25R)</div>
                    <div style="font-weight:700; color:#10b981;">${tp2:,.2f}</div>
                </div>
            </div>
            <div style="margin-top:12px; padding-top:8px; border-top:1px solid #1e293b; display:flex; justify-content:space-between; font-size:10px; color:#94a3b8;">
                <span>Risk/Coin: <b>${risk_usd:,.2f}</b></span>
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
            background: rgba(15, 23, 42, 0.9);
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 10px 16px;
            color: #94a3b8;
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            backdrop-filter: blur(8px);
        ">
            <span style="color:#38bdf8; font-weight:600;">● SCANNING 15M SMC STRUCTURE</span> · Standby
        </div>
        """

    html_code = f"""
    <div style="position: relative; width: 100%; height: 560px; border: 1px solid #1e293b; border-radius: 12px; overflow: hidden; background: #07090e;">
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
            "backgroundColor": "rgba(7, 9, 14, 1)",
            "gridColor": "rgba(30, 41, 59, 0.3)",
            "hide_top_toolbar": false,
            "hide_legend": false,
            "save_image": false,
            "container_id": "tv_chart_container"
        }});
        </script>
    </div>
    """
    components.html(html_code, height=575)


def main() -> None:
    head_left, head_right = st.columns([3, 1])
    with head_left:
        st.markdown("### ⚡ SMC Institutional BTC/USDT Terminal")
        st.caption("Live Coinbase/Binance Feed · SMC Liquidity Model · Delta Exchange Automated Execution")
    with head_right:
        live_sync = st.toggle("Live Auto-Sync (5s)", value=True)

    payload = fetch_vault_payload()
    if not payload:
        st.warning("Connecting to Render Backend... Please ensure service is awake.")
        if live_sync:
            time.sleep(AUTO_REFRESH_SECONDS)
            st.rerun()
        return

    # Data Extraction
    spot_price = float(payload.get("price") or 0.0)
    active_session = payload.get("session", "ACTIVE")
    realized_pnl = float(payload.get("realized_pnl_usd", 0.0))
    equity_est = float(payload.get("equity_estimate_usd", 1000.0))
    open_trades_count = payload.get("open_trades", 0)
    stats = payload.get("stats", {"wins": 0, "losses": 0, "win_rate": 0.0, "total_r": 0.0})

    active_list = payload.get("active", [])
    active_trade = active_list[0] if active_list else payload.get("active_trade")
    vault_trades = payload.get("vault", [])

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
        pnl_color = "#10b981" if realized_pnl >= 0 else "#ef4444"
        st.markdown(
            f"""
            <div class="metric-card" style="border-left-color: {pnl_color};">
                <div class="metric-label">Realized PnL (USD)</div>
                <div class="metric-value" style="color: {pnl_color};">${realized_pnl:+,.2f}</div>
                <div class="metric-sub">Est. Equity: ${equity_est:,.2f}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"""
            <div class="metric-card" style="border-left-color: #6366f1;">
                <div class="metric-label">Active Exposure</div>
                <div class="metric-value">{open_trades_count} Position</div>
                <div class="metric-sub">Risk Floor: $5.00 Enforced</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c4:
        total_r = stats.get("total_r", 0.0)
        win_rate = stats.get("win_rate", 0.0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        st.markdown(
            f"""
            <div class="metric-card" style="border-left-color: #10b981;">
                <div class="metric-label">Vault Performance</div>
                <div class="metric-value">{total_r:+.2f}R</div>
                <div class="metric-sub">Win Rate: {win_rate}% ({wins}W / {losses}L)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.write("")

    # 2. TRADINGVIEW CHART WITH OVERLAY
    render_chart_with_overlay(active_trade, spot_price)

    st.write("")
    st.markdown("#### 🏛️ Institutional Trade Vault History")

    # 3. HISTORICAL VAULT TABLE
    if vault_trades:
        formatted_rows = []
        for row in vault_trades:
            pnl_r = row.get("weighted_pnl_r")
            formatted_rows.append(
                {
                    "Time (UTC)": row.get("opened_at_utc", "")[:19].replace("T", " "),
                    "Direction": row.get("direction", ""),
                    "Setup": row.get("setup_name", ""),
                    "Score": f"{float(row.get('score', 0)):.0f}",
                    "Entry": f"${float(row.get('entry_price', 0)):,.2f}",
                    "SL": f"${float(row.get('stop_price', 0)):,.2f}",
                    "TP1": f"${float(row.get('tp1_price', 0)):,.2f}",
                    "Outcome": row.get("close_reason") or row.get("stage", "ACTIVE"),
                    "Net PnL ($)": f"${float(row.get('realized_pnl_usd', 0)):+.2f}",
                    "PnL (R)": f"{float(pnl_r if pnl_r is not None else 0):+.2f}R",
                    "Status": row.get("stage", "OPEN"),
                }
            )
        vault_df = pd.DataFrame(formatted_rows)
        st.dataframe(vault_df, use_container_width=True, hide_index=True)
    else:
        st.info("ℹ️ No historical trades in vault yet. Scanner engine is actively monitoring 15M liquidity zones.")

    if live_sync:
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()


if __name__ == "__main__":
    main()
