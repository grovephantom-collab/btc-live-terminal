import os, time, requests, logging, threading
from typing import Dict, Any
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

USD_INR = float(getattr(config, "USD_INR_RATE", 86.0))
_LOCAL_LOCK = threading.Lock()
_LOCAL_STRUCT: Dict[str, Dict[str, Any]] = {}
_LAST_FETCH_TS = 0.0
_CACHED_PAYLOAD: Dict[str, Any] = {}

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

def _bg_worker_loop():
    """Background worker so UI never freezes or blinks during market refreshes."""
    while True:
        try:
            market_data.refresh_all_market_data(_LOCAL_LOCK)
            for sym in config.SYMBOLS:
                with _LOCAL_LOCK:
                    m = dict(market_data.get_market(sym))
                st_info = smc_engine.get_structure(m, sym)
                if st_info:
                    with _LOCAL_LOCK:
                        _LOCAL_STRUCT[sym] = st_info
        except Exception as e:
            log.warning("Background refresh error: %s", e)
        time.sleep(10)

@st.cache_resource
def _start_background_engine():
    database.init_db()
    market_data.refresh_all_market_data(_LOCAL_LOCK)
    for sym in config.SYMBOLS:
        with _LOCAL_LOCK:
            m = dict(market_data.get_market(sym))
        st_info = smc_engine.get_structure(m, sym)
        if st_info:
            _LOCAL_STRUCT[sym] = st_info
    t = threading.Thread(target=_bg_worker_loop, daemon=True)
    t.start()
    return True

def _build_local_payload() -> Dict[str, Any]:
    _start_background_engine()
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
            with _LOCAL_LOCK:
                m = dict(market_data.get_market(sym))
                st_info = dict(_LOCAL_STRUCT.get(sym, {}))
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
    global _LAST_FETCH_TS, _CACHED_PAYLOAD
    now = time.time()
    if _CACHED_PAYLOAD and (now - _LAST_FETCH_TS) < 4.0:
        return _CACHED_PAYLOAD

    backend_base = _get_backend_url()
    if backend_base:
        try:
            resp = requests.get(f"{backend_base}/vault-data", timeout=4)
            if resp.status_code == 200:
                _CACHED_PAYLOAD = resp.json()
                _LAST_FETCH_TS = now
                return _CACHED_PAYLOAD
        except Exception:
            pass
    try:
        resp = requests.get(f"http://127.0.0.1:{config.PORT}/vault-data", timeout=1.5)
        if resp.status_code == 200:
            _CACHED_PAYLOAD = resp.json()
            _LAST_FETCH_TS = now
            return _CACHED_PAYLOAD
    except Exception:
        pass

    _CACHED_PAYLOAD = _build_local_payload()
    _LAST_FETCH_TS = now
    return _CACHED_PAYLOAD

def toggle_bot_state() -> None:
    global _LAST_FETCH_TS
    _LAST_FETCH_TS = 0.0
    backend_base = _get_backend_url()
    if backend_base:
        try:
            requests.post(f"{backend_base}/toggle-bot", timeout=4)
            return
        except Exception:
            pass
    cur = database.get_setting("auto_trade_enabled", "1")
    database.set_setting("auto_trade_enabled", "0" if cur == "1" else "1")

# ANTI-BLINK & MOBILE-FIRST PRO UI CSS
st.markdown(
    """
    <style>
        /* Stop Streamlit screen dimming / blinking on update */
        [data-stale="true"], .stApp [data-stale="true"], div[data-testid="stAppViewBlockContainer"] * {
            opacity: 1 !important;
            transition: none !important;
            filter: none !important;
        }
        div[data-testid="stStatusWidget"] { display: none !important; }

        body, .stApp {
            background-color: #050811;
            color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .block-container {
            padding-top: 0.8rem !important;
            padding-bottom: 2rem !important;
            max-width: 1120px;
        }
        .top-bar {
            background: linear-gradient(135deg, #0f172a 0%, #090d16 100%);
            border: 1px solid #1e293b;
            border-radius: 14px;
            padding: 14px 16px;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }
        .brand-title {
            font-size: 1.05rem;
            font-weight: 800;
            letter-spacing: 0.04em;
            color: #f8fafc;
        }
        .brand-sub {
            font-size: 0.72rem;
            color: #94a3b8;
            margin-top: 2px;
        }
        .status-pill {
            padding: 5px 11px;
            border-radius: 20px;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.03em;
        }
        /* Compact 2x2 Grid for Mobile & 4-col for Desktop */
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 16px;
        }
        @media (min-width: 768px) {
            .kpi-grid { grid-template-columns: repeat(4, 1fr); }
        }
        .kpi-box {
            background: #0b1120;
            border: 1px solid #1e293b;
            border-radius: 12px;
            padding: 12px 14px;
        }
        .kpi-title {
            color: #94a3b8;
            font-size: 0.68rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 4px;
        }
        .kpi-val {
            font-size: 1.25rem;
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            color: #38bdf8;
        }
        .kpi-sub {
            color: #64748b;
            font-size: 0.72rem;
            margin-top: 3px;
        }
        .sec-title {
            font-size: 0.95rem;
            font-weight: 700;
            color: #e2e8f0;
            margin: 14px 0 8px 0;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        /* 2x2 Market Scanner Grid on Mobile */
        .market-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 16px;
        }
        @media (min-width: 900px) {
            .market-grid { grid-template-columns: repeat(4, 1fr); }
        }
        .mkt-card {
            background: #0b1120;
            border: 1px solid #1e293b;
            border-radius: 12px;
            padding: 12px 14px;
        }
        .mkt-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }
        .mkt-name {
            font-size: 0.75rem;
            font-weight: 700;
            color: #cbd5e1;
        }
        .mkt-price {
            font-size: 1.2rem;
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            margin: 4px 0 8px 0;
        }
        .tag-bull { background: rgba(16,185,129,0.15); color: #10b981; border: 1px solid rgba(16,185,129,0.4); padding: 2px 7px; border-radius: 6px; font-size: 0.65rem; font-weight: 700; }
        .tag-bear { background: rgba(239,68,68,0.15); color: #ef4444; border: 1px solid rgba(239,68,68,0.4); padding: 2px 7px; border-radius: 6px; font-size: 0.65rem; font-weight: 700; }
        .tag-neut { background: rgba(245,158,11,0.15); color: #f59e0b; border: 1px solid rgba(245,158,11,0.4); padding: 2px 7px; border-radius: 6px; font-size: 0.65rem; font-weight: 700; }
        .mkt-row {
            display: flex;
            justify-content: space-between;
            font-size: 0.70rem;
            color: #94a3b8;
            border-top: 1px solid #162032;
            padding-top: 5px;
            margin-top: 5px;
        }
        .pos-card {
            background: #0f172a;
            border-left: 4px solid #38bdf8;
            border-radius: 10px;
            padding: 12px 14px;
            margin-bottom: 10px;
            border-top: 1px solid #1e293b;
            border-right: 1px solid #1e293b;
            border-bottom: 1px solid #1e293b;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

def _render_dashboard_body():
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

    # 1. TOP BAR
    s_bg = "rgba(16,185,129,0.15)" if auto_on else "rgba(239,68,68,0.15)"
    s_bd = "#10b981" if auto_on else "#ef4444"
    s_tx = f"● {mode} ACTIVE" if auto_on else "⏸ PAUSED"
    kz_label = payload.get("killzone_name", "OFF_HOURS").replace("_", " ")

    st.markdown(
        f'<div class="top-bar">'
        f'<div><div class="brand-title">⚡ SR QUANTX PRO</div>'
        f'<div class="brand-sub">Session: {payload.get("session", "ACTIVE")} • {kz_label}</div></div>'
        f'<div class="status-pill" style="background:{s_bg};border:1px solid {s_bd};color:{s_bd};">{s_tx}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 2. COMPACT 2x2 ACCOUNT CARDS
    pnl_clr = "#10b981" if realized_pnl >= 0 else "#ef4444"
    upnl_clr = "#10b981" if unrealized_pnl >= 0 else "#ef4444"
    st.markdown(
        f'<div class="kpi-grid">'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">💰 कुल बैलेंस (Wallet)</div>'
        f'<div class="kpi-val">${bal_usd:,.2f}</div>'
        f'<div class="kpi-sub">₹{bal_usd * USD_INR:,.0f} • Free: ${avail_usd:,.2f}</div>'
        f'</div>'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">📊 कुल प्रॉफिट (Net PnL)</div>'
        f'<div class="kpi-val" style="color:{pnl_clr};">${realized_pnl:+.2f}</div>'
        f'<div class="kpi-sub">₹{realized_pnl * USD_INR:+,.0f} • कुल बंद ट्रेड</div>'
        f'</div>'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">⚡ चालू ट्रेड (Open PnL)</div>'
        f'<div class="kpi-val" style="color:{upnl_clr};">${unrealized_pnl:+.2f}</div>'
        f'<div class="kpi-sub">{len(active_list)}/{config.MAX_OPEN_TRADES} Active Positions</div>'
        f'</div>'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">🎯 एक्यूरेसी (Win Rate)</div>'
        f'<div class="kpi-val">{stats.get("win_rate", 0.0):.1f}%</div>'
        f'<div class="kpi-sub">Jeet: {stats.get("wins", 0)} | Loss: {stats.get("losses", 0)}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 3. 2x2 MULTI-MARKET SCANNER MATRIX
    st.markdown('<div class="sec-title">📡 Live Market Scanner (4 Markets)</div>', unsafe_allow_html=True)
    if markets:
        cards_html = '<div class="market-grid">'
        for sym, m in markets.items():
            p = float(m.get("price") or 0.0)
            bias = m.get("macro_bias", "NEUTRAL")
            adx = float(m.get("adx") or 0.0)
            cvd = float(m.get("cvd_pct") or 50.0)
            rsi = float(m.get("rsi") or 50.0)

            if bias == "BULLISH":
                t_cls, t_txt = "tag-bull", "▲ UPTREND (BUY)"
            elif bias == "BEARISH":
                t_cls, t_txt = "tag-bear", "▼ DOWNTREND (SELL)"
            else:
                t_cls, t_txt = "tag-neut", "• SIDEWAYS (WAIT)"

            p_clr = "#fbbf24" if "PAXG" in sym else "#f8fafc"
            short_name = m.get("display_name", sym).split("(")[0].strip()
            strength = "Strong 🔥" if adx >= 25 else "Normal"
            flow = f"Buyers {cvd:.0f}%" if cvd >= 50 else f"Sellers {100-cvd:.0f}%"

            cards_html += (
                f'<div class="mkt-card">'
                f'<div class="mkt-head"><span class="mkt-name">{short_name}</span><span class="{t_cls}">{t_txt}</span></div>'
                f'<div class="mkt-price" style="color:{p_clr};">${p:,.2f}</div>'
                f'<div class="mkt-row"><span>Trend Power:</span><b style="color:#e2e8f0;">{strength} ({adx:.0f})</b></div>'
                f'<div class="mkt-row"><span>Orderflow:</span><b style="color:#38bdf8;">{flow}</b></div>'
                f'<div class="mkt-row"><span>RSI Level:</span><b>{rsi:.1f}</b></div>'
                f'</div>'
            )
        cards_html += '</div>'
        st.markdown(cards_html, unsafe_allow_html=True)

    # 4. ACTIVE POSITIONS
    st.markdown(f'<div class="sec-title">⚡ Chal Raha Trade (Active: {len(active_list)}/{config.MAX_OPEN_TRADES})</div>', unsafe_allow_html=True)
    if active_list:
        for tr in active_list:
            upnl = float(tr.get("unrealized_pnl_usd") or 0.0)
            u_clr = "#10b981" if upnl >= 0 else "#ef4444"
            side = tr.get("direction", "LONG")
            b_clr = "#10b981" if side == "LONG" else "#ef4444"
            st.markdown(
                f'<div class="pos-card" style="border-left-color:{b_clr};">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                f'<b style="font-size:0.9rem;">{tr.get("symbol")} • {side} ({tr.get("setup_name")})</b>'
                f'<span style="color:{u_clr};font-weight:800;font-size:0.95rem;">${upnl:+.2f} (₹{upnl * USD_INR:+,.0f})</span>'
                f'</div>'
                f'<div style="font-size:0.76rem;color:#94a3b8;margin-top:6px;">'
                f'Entry: <b>${float(tr["entry_price"]):,.2f}</b> &nbsp;|&nbsp; '
                f'SL: <b style="color:#ef4444;">${float(tr["stop_price"]):,.2f}</b> &nbsp;|&nbsp; '
                f'TP1: <b style="color:#10b981;">${float(tr["tp1_price"]):,.2f}</b> &nbsp;|&nbsp; '
                f'TP2: <b style="color:#10b981;">${float(tr["tp2_price"]):,.2f}</b>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.caption("✅ Abhi koi trade open nahi hai — Bot 4 markets scan kar raha hai, pakka signal milte hi trade lega.")

    # 5. EQUITY CURVE & TRADE LOG IN CLEAN TABS
    tab1, tab2 = st.tabs(["📜 Trade History (पिछले ट्रेड)", "📈 Account Growth Chart"])
    with tab1:
        if vault_trades:
            df_log = pd.DataFrame([
                {
                    "Time": str(t.get("opened_at_utc", ""))[11:19],
                    "Market": str(t.get("symbol", "")).replace("USDT", ""),
                    "Side": t.get("direction"),
                    "Entry": f"${float(t.get('entry_price') or 0):,.2f}",
                    "SL": f"${float(t.get('stop_price') or 0):,.2f}",
                    "Result": t.get("close_reason") or "ACTIVE",
                    "PnL ($)": f"${float(t.get('realized_pnl_usd') or 0.0):+.2f}",
                }
                for t in vault_trades
            ])
            st.dataframe(df_log, use_container_width=True, hide_index=True)
        else:
            st.caption("Abhi tak koi trade record nahi hua hai.")

    with tab2:
        if len(eq_curve) > 1:
            df_eq = pd.DataFrame(eq_curve).set_index("trade")
            st.line_chart(df_eq[["equity"]], height=200)
        else:
            st.caption("Pehla trade close hone ke baad yahan profit growth chart dikhega.")

def main() -> None:
    if hasattr(st, "fragment"):
        @st.fragment(run_every="15s")
        def _live_fragment():
            _render_dashboard_body()
            c1, c2 = st.columns(2)
            with c1:
                if st.button("⏯ Pause / Resume Auto-Bot", use_container_width=True):
                    toggle_bot_state()
                    st.rerun()
            with c2:
                if st.button("🔄 Refresh Now", use_container_width=True):
                    global _LAST_FETCH_TS
                    _LAST_FETCH_TS = 0.0
                    st.rerun()
        _live_fragment()
    else:
        _render_dashboard_body()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("⏯ Pause / Resume Auto-Bot", use_container_width=True):
                toggle_bot_state()
                st.rerun()
        with c2:
            if st.button("🔄 Refresh Now", use_container_width=True):
                st.rerun()

if __name__ == "__main__":
    main()
