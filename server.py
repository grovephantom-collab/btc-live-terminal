#!/usr/bin/env python3
"""
MOBILE-FIRST MINIMALIST SMC TERMINAL (server.py - Streamlit)
"""

import os
import time
from typing import Dict, Any, Optional

import requests
import pandas as pd
import streamlit as st

BACKEND_URL = os.getenv("BACKEND_URL", "https://btc-live-terminal.onrender.com").rstrip("/")
VAULT_ENDPOINT = f"{BACKEND_URL}/vault-data"
AUTO_REFRESH_SECONDS = int(os.getenv("AUTO_REFRESH_SECONDS", "5"))
USD_INR = float(os.getenv("USD_INR_RATE", "86.0"))

st.set_page_config(
    page_title="BTCUSDT SMC TRADING TERMINAL",
    page_icon="⚡",
    layout="centered",
    initial_sidebar_state="collapsed",
)

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
    st.markdown(
        '<div class="terminal-header">'
        '<div>'
        '<div style="font-size: 1.05rem; font-weight: 700; font-family: \'JetBrains Mono\', monospace;">BTCUSDT SMC TRADING TERMINAL</div>'
        '<div style="font-size: 0.75rem; color: #94a3b8; margin-top: 2px;">Stock Learner + Booming Bulls Confluence | Delta Small-Cap</div>'
        '</div>'
        '<div style="background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 4px 10px; border-radius: 6px; font-size: 0.7rem; font-weight: 600; font-family: \'JetBrains Mono\', monospace;">● ENGINE ONLINE</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    payload = fetch_vault_payload()
    if not payload:
        st.warning("Connecting to Render Backend... Please wait.")
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()
        return

    spot_price = float(payload.get("price") or 0.0)
    active_session = payload.get("session", "ACTIVE")
    realized_pnl = float(payload.get("realized_pnl_usd", 0.0))
    realized_inr = realized_pnl * USD_INR
    stats = payload.get("stats", {"wins": 0, "losses": 0, "win_rate": 0.0, "total_r": 0.0})

    active_list = payload.get("active", [])
    active_trade = active_list[0] if active_list else None
    vault_trades = payload.get("vault", [])

    st.markdown(
        f'<div class="metric-card"><div class="metric-label">BITCOIN PRICE</div><div class="metric-value">${spot_price:,.2f}</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        f'<div class="metric-card"><div class="metric-label">DEALING SESSION</div><div style="color: #f59e0b; font-size: 1.25rem; font-weight: 700; font-family: \'JetBrains Mono\', monospace;">{active_session}</div></div>',
        unsafe_allow_html=True,
    )

    pnl_class = "metric-value-green" if realized_pnl >= 0 else "metric-value"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">REALIZED NET PNL</div><div class="{pnl_class}">${realized_pnl:+,.2f} USD <span style="font-size: 1rem; color: #94a3b8;">(₹{realized_inr:+,.0f})</span></div></div>',
        unsafe_allow_html=True,
    )

    win_rate = stats.get("win_rate", 0.0)
    total_t = stats.get("wins", 0) + stats.get("losses", 0)
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">WIN RATE / TRADES</div><div class="metric-value">{win_rate}% <span style="font-size: 1rem; color: #94a3b8;">({total_t}T)</span></div></div>',
        unsafe_allow_html=True,
    )

    if active_trade and active_trade.get("stage") != "CLOSED":
        dir_val = active_trade.get("direction", "LONG")
        setup_val = active_trade.get("setup_name", "SMC Confluence")
        entry_val = float(active_trade.get("entry_price", 0))
        sl_val = float(active_trade.get("stop_price", 0))
        tp_val = float(active_trade.get("tp1_price", 0))
        qty_val = float(active_trade.get("remaining_qty", 0.001))
        lots_val = max(1, int(round(qty_val / 0.001)))
        active_html = (
            f"<b>ACTIVE {dir_val} ({lots_val} Lot | {setup_val})</b><br>"
            f"Entry: ${entry_val:,.2f} | SL: ${sl_val:,.2f} | TP1: ${tp_val:,.2f}"
        )
    else:
        active_html = "No active trade open. Waiting for confirmed closed 15M candle (1H + 15M Liquidity Sweep or EMA20 Pullback)."

    st.markdown(
        f'<div class="metric-card">'
        f'<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">'
        f'<span class="metric-label" style="margin-bottom:0;">ACTIVE POSITION STATUS</span>'
        f'<span style="font-size: 0.65rem; color: #64748b; font-family: \'JetBrains Mono\', monospace;">45m SL Cooldown | 1-3 Lots</span>'
        f'</div>'
        f'<div style="font-size: 0.85rem; color: #cbd5e1; margin-top: 8px; line-height: 1.4;">{active_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="table-container"><div class="metric-label" style="margin-bottom: 12px;">VAULT TRADE HISTORY</div>', unsafe_allow_html=True)

    if vault_trades:
        rows = []
        for row in vault_trades:
            pnl_u = float(row.get("realized_pnl_usd", 0))
            rows.append({
                "Time": row.get("opened_at_utc", "")[11:19],
                "Side": row.get("direction", ""),
                "Entry": f"${float(row.get('entry_price', 0)):,.0f}",
                "Reason": row.get("close_reason") or row.get("stage", "ACTIVE"),
                "PnL": f"${pnl_u:+.2f} (₹{pnl_u * USD_INR:+.0f})"
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.markdown(
            '<div style="color: #64748b; font-size: 0.8rem; font-family: \'JetBrains Mono\', monospace; padding: 10px 0;">No past trades logged yet. Scanner is standing by setup execution.</div>',
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    time.sleep(AUTO_REFRESH_SECONDS)
    st.rerun()

if __name__ == "__main__":
    main()
