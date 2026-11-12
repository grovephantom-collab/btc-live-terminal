import os, time, requests, logging, threading
from typing import Dict, Any, Optional
import streamlit as st
import pandas as pd
import config, database, market_data, smc_engine, trade_manager, notifications

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("Dashboard")

st.set_page_config(
    page_title="SR QuantX — Pro Algo Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

AUTO_REFRESH_SECONDS = 8
USD_INR = float(getattr(config, "USD_INR_RATE", 86.0))
_LOCAL_LOCK = threading.Lock()
_LOCAL_STRUCT = {}

def _get_backend_url() -> str:
    url = os.getenv("BACKEND_URL", "").strip()
    if not url:
        try:
            url = str(st.secrets.get("BACKEND_URL", "")).strip()
        except Exception:
            url = ""
    if "aapka-render" in url or "your-render" in url or "example" in url:
        url = ""
    return url.rstrip("/")

@st.cache_resource
def _ensure_local_data_ready():
    database.init_db()
    market_data.refresh_all_market_data(_LOCAL_LOCK)
    return True

def _build_local_payload() -> Dict[str, Any]:
    _ensure_local_data_ready()
    market_data.refresh_all_market_data(_LOCAL_LOCK)
    c = database.db()
    try:
        ts = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()]
        active_rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 10").fetchall()]
        pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()["p"] or 0.0)
        unrealized = float(c.execute("SELECT COALESCE(SUM(unrealized_pnl_usd),0) u FROM trades WHERE stage != 'CLOSED'").fetchone()["u"] or 0.0)

        closed_asc = c.execute("SELECT symbol, realized_pnl_usd, closed_at_utc FROM trades WHERE stage = 'CLOSED' ORDER BY id ASC").fetchall()
        wins = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) > 0]
        losses = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) <= 0]
        total_closed = len(wins) + len(losses)
        win_rate = round((len(wins) / total_closed) * 100.0, 1) if total_closed > 0 else 0.0
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0.0)

        eq = float(config.START_EQUITY)
        peak = eq
        max_dd = 0.0
        equity_curve = [{"trade": 0, "equity": round(eq, 2), "drawdown": 0.0}]
        for idx, r in enumerate(closed_asc, start=1):
            eq += float(r["realized_pnl_usd"] or 0.0)
            if eq > peak:
                peak = eq
            dd = round(((peak - eq) / peak) * 100.0, 2) if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            equity_curve.append({"trade": idx, "equity": round(eq, 2), "drawdown": -dd})

        delta_wallet = trade_manager.fetch_delta_wallet_balance()
        wallet_bal = delta_wallet["balance"] if delta_wallet["balance"] is not None else round(config.START_EQUITY + pnl, 2)
        wallet_avail = delta_wallet["available"] if delta_wallet["available"] is not None else round(wallet_bal, 2)
        auto_enabled = database.get_setting("auto_trade_enabled", "1") == "1"

        markets_info = {}
        for sym in config.SYMBOLS:
            m = dict(market_data.get_market(sym))
            st_info = smc_engine.get_structure(m, sym) or {}
            params = config.get_asset_params(sym)
            markets_info[sym] = {
                "symbol": sym,
                "display_name": params["display_name"],
                "price": m.get("price"),
                "session": m.get("session", "ACTIVE"),
                "in_killzone": m.get("in_killzone", False),
                "killzone_name": m.get("killzone_name", "OFF_HOURS"),
                "funding_rate": m.get("funding_rate", 0.0001),
                "pdh": m.get("pdh"),
                "pdl": m.get("pdl"),
                "macro_bias": st_info.get("macro_bias", "NEUTRAL"),
                "adx": round(float(st_info.get("adx", 0.0)), 1),
                "rsi": round(float(st_info.get("rsi", 50.0)), 1),
                "cvd_pct": round(float(st_info.get("cvd_ratio", 0.5)) * 100.0, 1),
                "choch_status": st_info.get("choch_status", "NONE"),
            }

        btc_m = market_data.get_market("BTCUSDT")
        return {
            "mode": config.MODE,
            "auto_trade_enabled": auto_enabled,
            "symbols": config.SYMBOLS,
            "markets": markets_info,
            "wallet": {
                "balance_usd": wallet_bal,
                "available_usd": wallet_avail,
                "is_live_synced": delta_wallet["balance"] is not None
            },
            "session": btc_m.get("session", "ACTIVE"),
            "killzone_name": btc_m.get("killzone_name", "OFF_HOURS"),
            "realized_pnl_usd": round(pnl, 2),
            "unrealized_pnl_usd": round(unrealized, 2),
            "stats": {
                "wins": len(wins),
                "losses": len(losses),
                "total_trades": total_closed,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "max_drawdown_pct": round(max_dd, 2),
            },
            "equity_curve": equity_curve,
            "vault": ts,
            "active": active_rows
        }
    finally:
        c.close()

def fetch_vault_payload() -> Dict[str, Any]:
    backend_base = _get_backend_url()
    if backend_base:
        try:
            resp = requests.get(f"{backend_base}/vault-data", timeout=8)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    try:
        resp = requests.get(f"http://127.0.0.1:{config.PORT}/vault-data", timeout=2)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return _build_local_payload()

def toggle_bot_state() -> None:
    backend_base = _get_backend_url()
    if backend_base:
        try:
            requests.post(f"{backend_base}/toggle-bot", timeout=5)
            return
        except Exception:
            pass
    cur = database.get_setting("auto_trade_enabled", "1")
    database.set_setting("auto_trade_enabled", "0" if cur == "1" else "1")

st.markdown(
    """
    <style>
        body, .stApp { background-color: #030712; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .block-container { padding-top: 1.0rem; padding-bottom: 2rem; max-width: 1180px; }
        .terminal-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 12px; margin-bottom: 16px; }
        .metric-card { background: #0b0f19; border: 1px solid #1e293b; border-radius: 12px; padding: 14px 16px; margin-bottom: 10px; }
        .metric-label { color: #94a3b8; font-size: 0.70rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 4px; }
        .metric-value { color: #38bdf8; font-size: 1.35rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .metric-sub { color: #94a3b8; font-size: 0.75rem; margin-top: 4px; }
        .badge-bull { color: #10b981; font-weight: 700; }
        .badge-bear { color: #ef4444; font-weight: 700; }
        .badge-neut { color: #f59e0b; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)

def main() -> None:
    payload = fetch_vault_payload()

    mode = payload.get("mode", "PAPER")
    auto_on = payload.get("auto_trade_enabled", True)
    wallet = payload.get("wallet", {})
    bal_usd = float(wallet.get("balance_usd") or config.START_EQUITY)
    avail_usd = float(wallet.get("available_usd") or bal_usd)
    realized_pnl = float(payload.get("realized_pnl_usd", 0.0))
    unrealized_pnl = float(payload.get("unrealized_pnl_usd", 0.0))
    stats = payload.get("stats", {})
    markets = payload.get("markets", {})
    active_list = payload.get("active", [])
    vault_trades = payload.get("vault", [])
    eq_curve = payload.get("equity_curve", [])

    h_col1, h_col2 = st.columns([4, 1])
    with h_col1:
        status_color = "#10b981" if auto_on else "#ef4444"
        status_text = f"● {mode} AUTO-TRADING ON" if auto_on else "⏸ AUTO-TRADING PAUSED"
        st.markdown(
            f'<div class="terminal-header">'
            f'<div><div style="font-size:1.15rem;font-weight:700;font-family:JetBrains Mono,monospace;">SR QUANTX — PRO ALGO TERMINAL</div>'
            f'<div style="font-size:0.78rem;color:#94a3b8;">24/7 Multi-Market Institutional SMC Engine | BTC • ETH • SOL • GOLD</div></div>'
            f'<div style="border:1px solid {status_color};color:{status_color};padding:5px 12px;border-radius:6px;font-size:0.75rem;font-weight:700;">{status_text}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with h_col2:
        btn_label = "⏸ Pause Bot" if auto_on else "▶ Resume Bot"
        if st.button(btn_label, use_container_width=True):
            toggle_bot_state()
            st.rerun()

    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(
        f'<div class="metric-card"><div class="metric-label">WALLET EQUITY {"(LIVE)" if wallet.get("is_live_synced") else "(SIM)"}</div>'
        f'<div class="metric-value">${bal_usd:,.2f}</div>'
        f'<div class="metric-sub">₹{bal_usd * USD_INR:,.0f} | Free: ${avail_usd:,.2f}</div></div>',
        unsafe_allow_html=True,
    )
    pnl_clr = "#10b981" if realized_pnl >= 0 else "#ef4444"
    k2.markdown(
        f'<div class="metric-card"><div class="metric-label">REALIZED / LIVE PNL</div>'
        f'<div class="metric-value" style="color:{pnl_clr};">${realized_pnl:+.2f}</div>'
        f'<div class="metric-sub">₹{realized_pnl * USD_INR:+,.0f} | Open: ${unrealized_pnl:+.2f}</div></div>',
        unsafe_allow_html=True,
    )
    k3.markdown(
        f'<div class="metric-card"><div class="metric-label">WIN RATE & PROFIT FACTOR</div>'
        f'<div class="metric-value">{stats.get("win_rate", 0.0):.1f}%</div>'
        f'<div class="metric-sub">{stats.get("wins", 0)}W / {stats.get("losses", 0)}L | PF: {stats.get("profit_factor", 0.0)}</div></div>',
        unsafe_allow_html=True,
    )
    k4.markdown(
        f'<div class="metric-card"><div class="metric-label">MAX DRAWDOWN & SESSION</div>'
        f'<div class="metric-value" style="color:#fbbf24;">-{stats.get("max_drawdown_pct", 0.0):.2f}%</div>'
        f'<div class="metric-sub">{payload.get("session", "ACTIVE")} ({payload.get("killzone_name", "OFF_HOURS")})</div></div>',
        unsafe_allow_html=True,
    )

    st.subheader("📡 Multi-Market Live Scanner Matrix")
    if markets:
        m_cols = st.columns(len(markets))
        for idx, (sym, m) in enumerate(markets.items()):
            p = float(m.get("price") or 0.0)
            bias = m.get("macro_bias", "SCANNING")
            b_cls = "badge-bull" if bias == "BULLISH" else ("badge-bear" if bias == "BEARISH" else "badge-neut")
            p_clr = "#fbbf24" if "PAXG" in sym else "#38bdf8"
            m_cols[idx].markdown(
                f'<div class="metric-card">'
                f'<div class="metric-label">{m.get("display_name", sym)}</div>'
                f'<div class="metric-value" style="color:{p_clr};">${p:,.2f}</div>'
                f'<div class="metric-sub">Bias: <span class="{b_cls}">{bias}</span> | ADX: {m.get("adx", 0):.1f}</div>'
                f'<div class="metric-sub">CVD: {m.get("cvd_pct", 50.0):.1f}% | RSI: {m.get("rsi", 50.0):.1f}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    c_left, c_right = st.columns([1.2, 1.0])
    with c_left:
        st.subheader(f"⚡ Active Positions ({len(active_list)}/{config.MAX_OPEN_TRADES})")
        if active_list:
            for tr in active_list:
                upnl = float(tr.get("unrealized_pnl_usd") or 0.0)
                u_clr = "#10b981" if upnl >= 0 else "#ef4444"
                st.markdown(
                    f'<div class="metric-card">'
                    f'<div class="metric-label">{tr.get("symbol")} • {tr.get("direction")} • {tr.get("setup_name")}</div>'
                    f'<div style="font-size:1.05rem;font-weight:700;color:{u_clr};">Live PnL: ${upnl:+.2f} (₹{upnl * USD_INR:+,.0f})</div>'
                    f'<div class="metric-sub">Entry: ${float(tr["entry_price"]):,.2f} | SL: ${float(tr["stop_price"]):,.2f} | TP1: ${float(tr["tp1_price"]):,.2f} | TP2: ${float(tr["tp2_price"]):,.2f}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("Scanner active across BTC, ETH, SOL & Gold — waiting for high-conviction institutional confluence.")

    with c_right:
        st.subheader("📈 Equity Curve (USD)")
        if len(eq_curve) > 1:
            df_eq = pd.DataFrame(eq_curve).set_index("trade")
            st.line_chart(df_eq[["equity"]], height=200)
        else:
            st.info("Equity curve will plot automatically after the first closed trade.")

    st.subheader("📜 Vault Trade Log")
    if vault_trades:
        df_log = pd.DataFrame([
            {
                "Time (UTC)": str(t.get("opened_at_utc", ""))[:19].replace("T", " "),
                "Symbol": t.get("symbol"),
                "Side": t.get("direction"),
                "Setup": t.get("setup_name"),
                "Entry": f"${float(t.get('entry_price') or 0):,.2f}",
                "SL": f"${float(t.get('stop_price') or 0):,.2f}",
                "Status": t.get("stage"),
                "Reason": t.get("close_reason") or "RUNNING",
                "PnL ($)": round(float(t.get("realized_pnl_usd") or 0.0), 2),
            }
            for t in vault_trades
        ])
        st.dataframe(df_log, use_container_width=True, hide_index=True)
    else:
        st.caption("No trades recorded yet.")

    time.sleep(AUTO_REFRESH_SECONDS)
    st.rerun()

if __name__ == "__main__":
    main()
