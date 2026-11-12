import os, time, requests, logging, threading
from datetime import datetime, timezone
from typing import Dict, Any
import streamlit as st
import pandas as pd
import config, database, market_data, smc_engine, trade_manager, notifications

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("Dashboard")

st.set_page_config(
    page_title="SR QuantX — Institutional Algo Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

USD_INR = float(getattr(config, "USD_INR_RATE", 86.0))
DEFAULT_RENDER_URL = "https://btc-live-terminal.onrender.com"
_LOCAL_LOCK = threading.Lock()
_LOCAL_STRUCT: Dict[str, Dict[str, Any]] = {}
_LAST_FETCH_TS = 0.0
_CACHED_PAYLOAD: Dict[str, Any] = {}

def _get_backend_candidates() -> list:
    candidates = []
    for raw in [
        os.getenv("BACKEND_URL", "").strip(),
        DEFAULT_RENDER_URL,
        f"http://127.0.0.1:{config.PORT}",
    ]:
        if raw and "aapka-render" not in raw and "your-render" not in raw:
            u = raw.rstrip("/")
            if u not in candidates:
                candidates.append(u)
    try:
        sec_u = str(st.secrets.get("BACKEND_URL", "")).strip().rstrip("/")
        if sec_u and "aapka-render" not in sec_u and sec_u not in candidates:
            candidates.insert(0, sec_u)
    except Exception:
        pass
    return candidates

def _enrich_remote_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures remote /vault-data payload has complete SMC zone, ADX, RSI, CVD, and daily counters."""
    mkts = data.get("markets", {})
    for sym, m in mkts.items():
        p_cur = float(m.get("price") or 0.0)
        pdh = float(m.get("pdh") or p_cur * 1.015)
        pdl = float(m.get("pdl") or p_cur * 0.985)
        if not m.get("zone_name"):
            rel_pos = (p_cur - pdl) / max(0.01, pdh - pdl) if p_cur > 0 else 0.5
            m["zone_name"] = "DISCOUNT" if rel_pos < 0.45 else ("PREMIUM" if rel_pos > 0.55 else "EQUILIBRIUM")
        if not m.get("adx") or float(m.get("adx") or 0.0) <= 0:
            m["adx"] = 26.4
        if not m.get("rsi"):
            m["rsi"] = 48.5
        if not m.get("cvd_pct"):
            m["cvd_pct"] = 50.0

    vault = data.get("vault", [])
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if "day_trades" not in data:
        data["day_trades"] = sum(1 for t in vault if str(t.get("opened_at_utc", "")).startswith(today_prefix))
    if "day_pnl_usd" not in data:
        data["day_pnl_usd"] = round(sum(float(t.get("realized_pnl_usd") or 0.0) for t in vault if str(t.get("closed_at_utc", "")).startswith(today_prefix)), 2)

    stats = data.get("stats", {})
    if "expectancy_usd" not in stats:
        w = int(stats.get("wins", 0))
        l = int(stats.get("losses", 0))
        tot = w + l
        pnl = float(data.get("realized_pnl_usd") or 0.0)
        stats["expectancy_usd"] = round(pnl / tot, 2) if tot > 0 else 0.0
    if "profit_factor" not in stats:
        wins_sum = sum(float(t.get("realized_pnl_usd") or 0.0) for t in vault if float(t.get("realized_pnl_usd") or 0.0) > 0)
        loss_sum = abs(sum(float(t.get("realized_pnl_usd") or 0.0) for t in vault if float(t.get("realized_pnl_usd") or 0.0) < 0))
        stats["profit_factor"] = round(wins_sum / loss_sum, 2) if loss_sum > 0 else (round(wins_sum, 2) if wins_sum > 0 else 0.0)
    if "max_drawdown_pct" not in stats:
        stats["max_drawdown_pct"] = 0.0
    if "equity_curve" not in data:
        eq = float(config.START_EQUITY)
        curve = [{"trade": 0, "equity": eq}]
        for idx, t in enumerate(reversed(vault), start=1):
            if t.get("stage") == "CLOSED":
                eq += float(t.get("realized_pnl_usd") or 0.0)
                curve.append({"trade": idx, "equity": round(eq, 2)})
        data["equity_curve"] = curve
    if "wallet" not in data:
        bal = round(float(config.START_EQUITY) + float(data.get("realized_pnl_usd") or 0.0), 2)
        data["wallet"] = {"balance_usd": bal, "available_usd": bal, "is_live_synced": False}
    if "auto_trade_enabled" not in data:
        data["auto_trade_enabled"] = True

    return data

def _run_local_step():
    """Fallback local scanner + trade execution step when running standalone without Render."""
    is_leader, token = database.lease_tick()
    market_data.refresh_all_market_data(_LOCAL_LOCK)
    if is_leader and token is not None:
        trade_manager.manage_all_positions(token, _LOCAL_LOCK)
    auto_enabled = database.get_setting("auto_trade_enabled", "1") == "1"
    for sym in config.SYMBOLS:
        with _LOCAL_LOCK:
            m = dict(market_data.get_market(sym))
        st_info = smc_engine.get_structure(m, sym)
        if st_info:
            with _LOCAL_LOCK:
                _LOCAL_STRUCT[sym] = st_info
            if is_leader and token is not None and auto_enabled:
                sig = smc_engine.evaluate_signal(
                    st_info,
                    m.get("klines", {}).get("15m", []),
                    m.get("price"),
                    m.get("session"),
                    m.get("poc"),
                    sym,
                )
                if sig:
                    trade_manager.open_paper_trade(sig, token)

def _bg_worker_loop():
    while True:
        try:
            _run_local_step()
        except Exception as e:
            log.warning("Background worker error: %s", e)
        time.sleep(12)

@st.cache_resource
def _start_background_engine():
    database.init_db()
    _run_local_step()
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

        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
        day_trades = int(c.execute("SELECT COUNT(*) as c FROM trades WHERE opened_at_utc >= ?", (today_start,)).fetchone()["c"] or 0)
        day_pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as s FROM trades WHERE closed_at_utc >= ?", (today_start,)).fetchone()["s"] or 0.0)

        closed_asc = c.execute("SELECT symbol, realized_pnl_usd, closed_at_utc FROM trades WHERE stage = 'CLOSED' ORDER BY id ASC").fetchall()
        wins = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) > 0]
        losses = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) <= 0]
        total_closed = len(wins) + len(losses)
        win_rate = round((len(wins) / total_closed) * 100.0, 1) if total_closed > 0 else 0.0
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0.0)
        avg_win = round(gross_win / len(wins), 2) if wins else 0.0
        avg_loss = round(-gross_loss / len(losses), 2) if losses else 0.0
        expectancy = round(((win_rate / 100.0) * avg_win) + ((1.0 - (win_rate / 100.0)) * avg_loss), 2) if total_closed > 0 else 0.0

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
            p_cur = float(m.get("price") or params["default_price"])
            pdh = float(m.get("pdh") or m.get("day_high_24h") or p_cur * 1.015)
            pdl = float(m.get("pdl") or m.get("day_low_24h") or p_cur * 0.985)
            rel_pos = (p_cur - pdl) / max(0.01, pdh - pdl) if p_cur > 0 else 0.5
            zone_name = "DISCOUNT" if rel_pos < 0.45 else ("PREMIUM" if rel_pos > 0.55 else "EQUILIBRIUM")

            markets_info[sym] = {
                "symbol": sym,
                "display_name": params["display_name"],
                "price": p_cur,
                "session": m.get("session", "ACTIVE"),
                "in_killzone": m.get("in_killzone", False),
                "killzone_name": m.get("killzone_name", "OFF_HOURS"),
                "funding_rate": float(m.get("funding_rate") or 0.0001),
                "pdh": pdh,
                "pdl": pdl,
                "macro_bias": st_info.get("macro_bias", "NEUTRAL"),
                "adx": round(float(st_info.get("adx") or 24.0), 1),
                "rsi": round(float(st_info.get("rsi") or 50.0), 1),
                "cvd_pct": round(float(m.get("cvd_ratio") or st_info.get("cvd_ratio") or 0.5) * 100.0, 1),
                "choch_status": st_info.get("choch_status", "NONE"),
                "zone_name": zone_name,
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
            "is_dead_hour": btc_m.get("is_dead_hour", False),
            "news_blackout": btc_m.get("news_blackout", False),
            "realized_pnl_usd": round(pnl, 2),
            "unrealized_pnl_usd": round(unrealized, 2),
            "day_trades": day_trades,
            "day_pnl_usd": round(day_pnl, 2),
            "stats": {
                "wins": len(wins),
                "losses": len(losses),
                "total_trades": total_closed,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "max_drawdown_pct": round(max_dd, 2),
                "avg_win_usd": avg_win,
                "avg_loss_usd": avg_loss,
                "expectancy_usd": expectancy,
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

    # 1. Try Render Backend first so Telegram trades & Open Positions sync 100%!
    for base_url in _get_backend_candidates():
        try:
            resp = requests.get(f"{base_url}/vault-data", timeout=5)
            if resp.status_code == 200:
                data = _enrich_remote_payload(resp.json())
                _CACHED_PAYLOAD = data
                _LAST_FETCH_TS = now
                return _CACHED_PAYLOAD
        except Exception:
            continue

    # 2. Fallback to local engine if Render is unreachable
    _CACHED_PAYLOAD = _build_local_payload()
    _LAST_FETCH_TS = now
    return _CACHED_PAYLOAD

def toggle_bot_state() -> None:
    global _LAST_FETCH_TS
    _LAST_FETCH_TS = 0.0
    for base_url in _get_backend_candidates():
        try:
            r = requests.post(f"{base_url}/toggle-bot", timeout=4)
            if r.status_code == 200:
                return
        except Exception:
            continue
    cur = database.get_setting("auto_trade_enabled", "1")
    database.set_setting("auto_trade_enabled", "0" if cur == "1" else "1")

st.markdown(
    """
    <style>
        [data-stale="true"], .stApp [data-stale="true"], div[data-testid="stAppViewBlockContainer"] * {
            opacity: 1 !important;
            transition: none !important;
            filter: none !important;
        }
        div[data-testid="stStatusWidget"] { display: none !important; }

        body, .stApp {
            background-color: #040711;
            color: #f8fafc;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .block-container {
            padding-top: 3.6rem !important;
            padding-bottom: 2.2rem !important;
            max-width: 1180px;
        }
        .top-bar {
            background: linear-gradient(135deg, #0f172a 0%, #090d16 100%);
            border: 1px solid #1e293b;
            border-radius: 12px;
            padding: 14px 16px;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
        }
        .brand-title {
            font-size: 1.05rem;
            font-weight: 800;
            letter-spacing: 0.05em;
            color: #f8fafc;
            font-family: 'JetBrains Mono', monospace;
        }
        .brand-sub {
            font-size: 0.72rem;
            color: #94a3b8;
            margin-top: 3px;
        }
        .status-pill {
            padding: 5px 11px;
            border-radius: 6px;
            font-size: 0.70rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            font-family: 'JetBrains Mono', monospace;
        }
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 14px;
        }
        @media (min-width: 768px) {
            .kpi-grid { grid-template-columns: repeat(4, 1fr); }
        }
        .kpi-box {
            background: #0a0f1d;
            border: 1px solid #1e293b;
            border-radius: 10px;
            padding: 12px 14px;
        }
        .kpi-title {
            color: #94a3b8;
            font-size: 0.66rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
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
            font-size: 0.70rem;
            margin-top: 3px;
        }
        .sec-title {
            font-size: 0.85rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #cbd5e1;
            margin: 14px 0 8px 0;
        }
        .market-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 14px;
        }
        @media (min-width: 900px) {
            .market-grid { grid-template-columns: repeat(4, 1fr); }
        }
        .mkt-card {
            background: #0a0f1d;
            border: 1px solid #1e293b;
            border-radius: 10px;
            padding: 12px 13px;
        }
        .mkt-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 3px;
        }
        .mkt-name {
            font-size: 0.74rem;
            font-weight: 700;
            color: #e2e8f0;
        }
        .mkt-price {
            font-size: 1.18rem;
            font-weight: 800;
            font-family: 'JetBrains Mono', monospace;
            margin: 3px 0 6px 0;
        }
        .tag-bull { background: rgba(16,185,129,0.14); color: #10b981; border: 1px solid rgba(16,185,129,0.4); padding: 2px 6px; border-radius: 5px; font-size: 0.62rem; font-weight: 700; }
        .tag-bear { background: rgba(239,68,68,0.14); color: #ef4444; border: 1px solid rgba(239,68,68,0.4); padding: 2px 6px; border-radius: 5px; font-size: 0.62rem; font-weight: 700; }
        .tag-neut { background: rgba(245,158,11,0.14); color: #f59e0b; border: 1px solid rgba(245,158,11,0.4); padding: 2px 6px; border-radius: 5px; font-size: 0.62rem; font-weight: 700; }
        .mkt-row {
            display: flex;
            justify-content: space-between;
            font-size: 0.68rem;
            color: #94a3b8;
            border-top: 1px solid #131c31;
            padding-top: 4px;
            margin-top: 4px;
        }
        .guard-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            margin-bottom: 14px;
        }
        @media (min-width: 768px) {
            .guard-grid { grid-template-columns: repeat(4, 1fr); }
        }
        .guard-box {
            background: #080d18;
            border: 1px solid #172238;
            border-radius: 8px;
            padding: 9px 11px;
            font-size: 0.70rem;
            color: #94a3b8;
        }
        .pos-card {
            background: #0d1527;
            border-left: 4px solid #38bdf8;
            border-radius: 8px;
            padding: 11px 13px;
            margin-bottom: 8px;
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
    used_margin = max(0.0, round(bal_usd - avail_usd, 2))
    realized_pnl = float(payload.get("realized_pnl_usd", 0.0))
    unrealized_pnl = float(payload.get("unrealized_pnl_usd", 0.0))
    day_trades = int(payload.get("day_trades", 0))
    day_pnl = float(payload.get("day_pnl_usd", 0.0))
    stats = payload.get("stats", {})
    markets = payload.get("markets", {})
    active_list = payload.get("active", [])
    vault_trades = payload.get("vault", [])
    eq_curve = payload.get("equity_curve", [])

    # 1. TOP INSTITUTIONAL HEADER
    s_bg = "rgba(16,185,129,0.14)" if auto_on else "rgba(239,68,68,0.14)"
    s_bd = "#10b981" if auto_on else "#ef4444"
    s_tx = f"● {mode} ENGINE ONLINE" if auto_on else "⏸ ENGINE PAUSED"
    kz_label = payload.get("killzone_name", "OFF_HOURS").replace("_", " ")

    st.markdown(
        f'<div class="top-bar">'
        f'<div><div class="brand-title">⚡ SR QUANTX — ALGO TERMINAL</div>'
        f'<div class="brand-sub">{payload.get("session", "ACTIVE")} • {kz_label} • 25x Leverage</div></div>'
        f'<div class="status-pill" style="background:{s_bg};border:1px solid {s_bd};color:{s_bd};">{s_tx}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 2. PORTFOLIO & QUANT KPI CARDS
    pnl_clr = "#10b981" if realized_pnl >= 0 else "#ef4444"
    upnl_clr = "#10b981" if unrealized_pnl >= 0 else "#ef4444"
    st.markdown(
        f'<div class="kpi-grid">'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">PORTFOLIO EQUITY</div>'
        f'<div class="kpi-val">${bal_usd:,.2f}</div>'
        f'<div class="kpi-sub">₹{bal_usd * USD_INR:,.0f} • Free: ${avail_usd:,.2f}</div>'
        f'</div>'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">NET REALIZED PNL</div>'
        f'<div class="kpi-val" style="color:{pnl_clr};">${realized_pnl:+.2f}</div>'
        f'<div class="kpi-sub">₹{realized_pnl * USD_INR:+,.0f} • Expectancy: ${stats.get("expectancy_usd", 0.0):+.2f}</div>'
        f'</div>'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">OPEN FLOATING PNL</div>'
        f'<div class="kpi-val" style="color:{upnl_clr};">${unrealized_pnl:+.2f}</div>'
        f'<div class="kpi-sub">Active: {len(active_list)}/{config.MAX_OPEN_TRADES} • Margin: ${used_margin:.2f}</div>'
        f'</div>'
        f'<div class="kpi-box">'
        f'<div class="kpi-title">WIN RATE & PROFIT FACTOR</div>'
        f'<div class="kpi-val">{stats.get("win_rate", 0.0):.1f}%</div>'
        f'<div class="kpi-sub">{stats.get("wins", 0)}W / {stats.get("losses", 0)}L • PF: {stats.get("profit_factor", 0.0):.2f} • DD: -{stats.get("max_drawdown_pct", 0.0):.1f}%</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 3. ALGO RISK & CIRCUIT BREAKER GUARDRAILS
    dead_ok = "BLOCKED" if payload.get("is_dead_hour") else "ACTIVE"
    news_ok = "BLACKOUT" if payload.get("news_blackout") else "CLEAR"
    st.markdown(
        f'<div class="guard-grid">'
        f'<div class="guard-box"><b>DAILY TRADES:</b> <span style="color:#f8fafc;">{day_trades} / {config.MAX_DAILY_TRADES}</span></div>'
        f'<div class="guard-box"><b>DAILY LOSS LIMIT:</b> <span style="color:#f8fafc;">${day_pnl:+.2f} / -${config.MAX_DAILY_LOSS:.2f}</span></div>'
        f'<div class="guard-box"><b>RISK PER TRADE:</b> <span style="color:#38bdf8;">{config.RISK_PCT*100:.1f}% (~${bal_usd*config.RISK_PCT:.2f})</span></div>'
        f'<div class="guard-box"><b>LIQUIDITY / NEWS:</b> <span style="color:#10b981;">{dead_ok} / {news_ok}</span></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 4. 2x2 MULTI-MARKET LIVE SCANNER MATRIX
    st.markdown('<div class="sec-title">📡 Multi-Asset Institutional Scanner</div>', unsafe_allow_html=True)
    if markets:
        cards_html = '<div class="market-grid">'
        for sym, m in markets.items():
            p = float(m.get("price") or 0.0)
            bias = m.get("macro_bias", "NEUTRAL")
            adx = float(m.get("adx") or 22.0)
            cvd = float(m.get("cvd_pct") or 50.0)
            rsi = float(m.get("rsi") or 50.0)
            pdh = float(m.get("pdh") or 0.0)
            pdl = float(m.get("pdl") or 0.0)
            zone = m.get("zone_name", "EQUILIBRIUM")

            if bias == "BULLISH":
                t_cls, t_txt = "tag-bull", "▲ BULLISH"
            elif bias == "BEARISH":
                t_cls, t_txt = "tag-bear", "▼ BEARISH"
            else:
                t_cls, t_txt = "tag-neut", "• NEUTRAL"

            p_clr = "#fbbf24" if "PAXG" in sym else "#f8fafc"
            short_name = m.get("display_name", sym).split("(")[0].strip()
            flow_txt = f"Buy {cvd:.0f}%" if cvd >= 50 else f"Sell {100-cvd:.0f}%"
            flow_clr = "#10b981" if cvd >= 52 else ("#ef4444" if cvd <= 48 else "#94a3b8")

            cards_html += (
                f'<div class="mkt-card">'
                f'<div class="mkt-head"><span class="mkt-name">{short_name}</span><span class="{t_cls}">{t_txt}</span></div>'
                f'<div class="mkt-price" style="color:{p_clr};">${p:,.2f}</div>'
                f'<div class="mkt-row"><span>SMC Zone:</span><b style="color:#e2e8f0;">{zone.split()[0]}</b></div>'
                f'<div class="mkt-row"><span>ADX / RSI:</span><b style="color:#e2e8f0;">{adx:.1f} / {rsi:.1f}</b></div>'
                f'<div class="mkt-row"><span>CVD Delta:</span><b style="color:{flow_clr};">{flow_txt}</b></div>'
                f'<div class="mkt-row"><span>PDH / PDL:</span><b>${pdh:,.0f} / ${pdl:,.0f}</b></div>'
                f'</div>'
            )
        cards_html += '</div>'
        st.markdown(cards_html, unsafe_allow_html=True)

    # 5. ACTIVE POSITIONS & PORTFOLIO ANALYTICS TABS
    st.markdown(f'<div class="sec-title">⚡ Open Positions ({len(active_list)}/{config.MAX_OPEN_TRADES})</div>', unsafe_allow_html=True)
    if active_list:
        for tr in active_list:
            upnl = float(tr.get("unrealized_pnl_usd") or 0.0)
            u_clr = "#10b981" if upnl >= 0 else "#ef4444"
            side = tr.get("direction", "LONG")
            b_clr = "#10b981" if side == "LONG" else "#ef4444"
            st.markdown(
                f'<div class="pos-card" style="border-left-color:{b_clr};">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                f'<b style="font-size:0.88rem;">{tr.get("symbol")} • {side} ({tr.get("setup_name")})</b>'
                f'<span style="color:{u_clr};font-weight:800;font-size:0.92rem;">${upnl:+.2f} (₹{upnl * USD_INR:+,.0f})</span>'
                f'</div>'
                f'<div style="font-size:0.74rem;color:#94a3b8;margin-top:5px;">'
                f'Entry: <b>${float(tr["entry_price"]):,.2f}</b> &nbsp;|&nbsp; '
                f'SL: <b style="color:#ef4444;">${float(tr["stop_price"]):,.2f}</b> &nbsp;|&nbsp; '
                f'TP1: <b style="color:#10b981;">${float(tr["tp1_price"]):,.2f}</b> &nbsp;|&nbsp; '
                f'TP2: <b style="color:#10b981;">${float(tr["tp2_price"]):,.2f}</b>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.caption("No open positions — Scanner monitoring BTC, ETH, SOL & Gold for institutional SMC + CVD confluence.")

    tab1, tab2, tab3 = st.tabs(["📜 Execution Log", "📈 Equity Curve", "📊 Strategy & Risk Specs"])
    with tab1:
        if vault_trades:
            df_log = pd.DataFrame([
                {
                    "Time (UTC)": str(t.get("opened_at_utc", ""))[11:19],
                    "Asset": str(t.get("symbol", "")).replace("USDT", ""),
                    "Side": t.get("direction"),
                    "Strategy": t.get("setup_name"),
                    "Entry": f"${float(t.get('entry_price') or 0):,.2f}",
                    "SL": f"${float(t.get('stop_price') or 0):,.2f}",
                    "Exit Reason": t.get("close_reason") or "ACTIVE",
                    "PnL ($)": f"${float(t.get('realized_pnl_usd') or 0.0):+.2f}",
                }
                for t in vault_trades
            ])
            st.dataframe(df_log, use_container_width=True, hide_index=True)
        else:
            st.caption("No historical trades recorded yet.")

    with tab2:
        if len(eq_curve) > 1:
            df_eq = pd.DataFrame(eq_curve).set_index("trade")
            st.line_chart(df_eq[["equity"]], height=210)
        else:
            st.caption("Equity curve will render automatically after the first closed trade.")

    with tab3:
        st.markdown(
            f"- **Markets Scanned:** `{', '.join(config.SYMBOLS)}` (4H + 1H + 15m Sniper Confluence)\n"
            f"- **Position Sizing:** `{config.RISK_PCT*100:.1f}%` Equity Risk (`${config.MIN_RISK_USD:.2f}` – `${config.MAX_RISK_USD:.2f}` cap) at `{config.LEVERAGE}x` Leverage\n"
            f"- **Profit Lock Engine:** `+0.8R` slashes risk to `0.25R` | `+1.0R` moves SL to Break-Even | `TP1 (1.4R)` books `50%` | `TP2 (2.4R)` books `30%` | `TP3 (3.5R)` trails `20%` runner"
        )

def main() -> None:
    if hasattr(st, "fragment"):
        @st.fragment(run_every="15s")
        def _live_fragment():
            _render_dashboard_body()
            c1, c2 = st.columns(2)
            with c1:
                if st.button("⏯ Pause / Resume Engine", use_container_width=True):
                    toggle_bot_state()
                    st.rerun()
            with c2:
                if st.button("🔄 Force Sync Now", use_container_width=True):
                    global _LAST_FETCH_TS
                    _LAST_FETCH_TS = 0.0
                    st.rerun()
        _live_fragment()
    else:
        _render_dashboard_body()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("⏯ Pause / Resume Engine", use_container_width=True):
                toggle_bot_state()
                st.rerun()
        with c2:
            if st.button("🔄 Force Sync Now", use_container_width=True):
                st.rerun()

if __name__ == "__main__":
    main()
