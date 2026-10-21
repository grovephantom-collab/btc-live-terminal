#!/usr/bin/env python3
"""
MOBILE-FIRST MINIMALIST SMC TERMINAL (server.py - Streamlit)
Designed specifically for mobile view without heavy charts.
"""

import os
import time
from typing import Dict, Any, Optional

import requests
import pandas as pd
import streamlit as st

# ==========================================
# CONFIGURATION
# ==========================================
BACKEND_URL = os.getenv("BACKEND_URL", "https://btc-live-terminal.onrender.com").rstrip("/")
VAULT_ENDPOINT = f"{BACKEND_URL}/vault-data"
AUTO_REFRESH_SECONDS = int(os.getenv("AUTO_REFRESH_SECONDS", "5"))

st.set_page_config(
    page_title="BTCUSDT SMC TRADING TERMINAL",
    page_icon="⚡",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ==========================================
# MOBILE-OPTIMIZED INSTITUTIONAL STYLING
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
            padding-bottom: 3rem;
            max-width: 600px;
        }
        .terminal-header {
            background: #0f172a;
            border: 1px solid #1e293b;
            border-radius: 12px;
            padding: 16px 20px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .metric-card {
            background: #0f172a;
            border: 1px solid #1e293b;
            border-radius: 12px;
            padding: 16px 20px;
            margin-bottom: 12px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }
        .metric-label {
            color: #94a3b8;
            font-size: 0.7rem;
            font-weight: 600;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 6px;
        }
        .metric-value {
            color: #38bdf8;
            font-size: 1.5rem;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }
        .metric-value-green {
            color: #10b981;
            font-size: 1.5rem;
            font-weight: 700;
            font-family: 'JetBrains Mono', monospace;
        }
        .sub-text {
            color: #64748b;
            font-size: 0.75rem;
            margin-top: 4px;
            font-family: 'JetBrains Mono', monospace;
        }
        .table-container {
            background: #0f172a;
            border: 1px solid #1e293b;
            border-radius: 12px;
            padding: 16px;
            margin-top: 16px;
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
    except Exception:
        return None


def main() -> None:
    # Top Header
    st.markdown(
        """
        <div class="terminal-header">
            <div>
                <div style="font-size: 1.05rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; letter-spacing: -0.01em;">BTCUSDT SMC TRADING TERMINAL</div>
                <div style="font-size: 0.75rem; color: #94a3b8; margin-top: 2px;">Institutional Orderflow Vault</div>
            </div>
            <div style="background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 4px 10px; border-radius: 6px; font-size: 0.7rem; font-weight: 600; font-family: 'JetBrains Mono', monospace;">
                ● ENGINE ONLINE
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    payload = fetch_vault_payload()
    if not payload:
        st.warning("Connecting to Render Backend... Please wait.")
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()
        return

    # Data Extraction
    spot_price = float(payload.get("price") or 0.0)
    active_session = payload.get("session", "ACTIVE")
    realized_pnl = float(payload.get("realized_pnl_usd", 0.0))
    open_trades_count = payload.get("open_trades", 0)
    stats = payload.get("stats", {"wins": 0, "losses": 0, "win_rate": 0.0, "total_r": 0.0})
    
    active_list = payload.get("active", [])
    active_trade = active_list[0] if active_list else payload.get("active_trade")
    vault_trades = payload.get("vault", [])

    # 1. BITCOIN PRICE
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">BITCOIN PRICE</div>
            <div class="metric-value">${spot_price:,.2f}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 2. DEALING SESSION
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">DEALING SESSION</div>
            <div style="color: #f59e0b; font-size: 1.25rem; font-weight: 700; font-family: 'JetBrains Mono', monospace;">{active_session}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 3. REALIZED NET PNL
    pnl_class = "metric-value-green" if realized_pnl >= 0 else "metric-value"
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">REALIZED NET PNL</div>
            <div class="{pnl_class}">${realized_pnl:+,.2f} USD</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 4. WIN RATE / TRADES
    win_rate = stats.get("win_rate", 0.0)
    total_t = stats.get("wins", 0) + stats.get("losses", 0)
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">WIN RATE / TRADES</div>
            <div class="metric-value">{win_rate}% <span style="font-size: 1rem; color: #94a3b8;">({total_t}T)</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 5. ACTIVE POSITION STATUS
    if active_trade:
        dir_val = active_trade.get("direction", "LONG")
        entry_val = float(active_trade.get("entry_price", 0))
        sl_val = float(active_trade.get("stop_price", 0))
        tp_val = float(active_trade.get("tp1_price", 0))
        active_html = f"""
        <b>ACTIVE {dir_val} POSITION</b><br>
        Entry: ${entry_val:,.2f} | SL: ${sl_val:,.2f} | TP: ${tp_val:,.2f}
        """
    else:
        active_html = "No active trade open. Strategy scanning closed 15M candles for Liquidity Sweeps & FVG CE mitigations."

    st.markdown(
        f"""
        <div class="metric-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span class="metric-label" style="margin-bottom:0;">ACTIVE POSITION STATUS</span>
                <span style="font-size: 0.65rem; color: #64748b; font-family: 'JetBrains Mono', monospace;">Max 1 Open Risk Cap</span>
            </div>
            <div style="font-size: 0.85rem; color: #cbd5e1; margin-top: 8px; line-height: 1.4;">
                {active_html}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 6. VAULT HISTORY TABLE
    st.markdown(
        """
        <div class="table-container">
            <div class="metric-label" style="margin-bottom: 12px;">VAULT TRADE HISTORY</div>
        """,
        unsafe_allow_html=True,
    )

    if vault_trades:
        rows = []
        for row in vault_trades:
            rows.append({
                "Time": row.get("opened_at_utc", "")[11:19],
                "Side": row.get("direction", ""),
                "Entry": f"${float(row.get('entry_price', 0)):,.0f}",
                "Reason": row.get("close_reason", "ACTIVE"),
                "PnL ($)": f"${float(row.get('realized_pnl_usd', 0)):+.2f}"
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.markdown(
            """
            <div style="color: #64748b; font-size: 0.8rem; font-family: 'JetBrains Mono', monospace; padding: 10px 0;">
                No past trades logged yet. Scanner is standing by setup execution.
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    time.sleep(AUTO_REFRESH_SECONDS)
    st.rerun()


if __name__ == "__main__":
    main()
