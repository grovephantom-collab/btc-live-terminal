import os, time, requests, logging, threading
from datetime import datetime, timezone
from typing import Dict, Any
import streamlit as st
import pandas as pd
import config, database, market_data, smc_engine, trade_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("Dashboard")

st.set_page_config(page_title="SR QuantX — Institutional Algo Terminal", page_icon="⚡", layout="wide", initial_sidebar_state="collapsed")
USD_INR = float(getattr(config, "USD_INR_RATE", 86.0))

def _fmt_p(val: float) -> str:
    v = float(val or 0.0)
    if v == 0:
        return "$0.00"
    if v < 0.005:
        return f"${v:,.6f}"
    return f"${v:,.5f}" if v < 1.0 else (f"${v:,.4f}" if v < 10.0 else (f"${v:,.3f}" if v < 100.0 else f"${v:,.2f}"))

DEFAULT_RENDER_URL = "https://btc-live-terminal.onrender.com"
_LOCAL_LOCK = threading.Lock()
_LOCAL_STRUCT: Dict[str, Dict[str, Any]] = {}
_LAST_FETCH_TS = 0.0
_CACHED_PAYLOAD: Dict[str, Any] = {}

def _get_backend_candidates() -> list:
    candidates = []
    for raw in [os.getenv("BACKEND_URL", "").strip(), DEFAULT_RENDER_URL, f"http://127.0.0.1:{config.PORT}"]:
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

def _calc_mover_heat_score(sym: str, m: Dict[str, Any], active_syms: set, recent_closed_syms: set) -> tuple:
    is_open = sym in active_syms
    was_just_closed = (sym in recent_closed_syms) and (not is_open)
    adx = float(m.get("adx") or 20.0)
    cvd = float(m.get("cvd_pct") or 50.0)
    bias = str(m.get("macro_bias") or "NEUTRAL")
    choch = str(m.get("choch_status") or "NONE")
    p = float(m.get("price") or 0.0)
    pdh, pdl = float(m.get("pdh") or 0.0), float(m.get("pdl") or 0.0)

    cvd_strength = abs(cvd - 50.0) * 1.5
    trend_bonus = 18.0 if bias in ("BULLISH", "BEARISH") else 0.0
    choch_bonus = 25.0 if choch != "NONE" else 0.0
    sweep_bonus = 0.0
    if p > 0 and pdh > pdl > 0:
        dist_hi = abs(p - pdh) / p
        dist_lo = abs(p - pdl) / p
        if min(dist_hi, dist_lo) <= 0.008:
            sweep_bonus = 22.0

    heat = adx + cvd_strength + trend_bonus + choch_bonus + sweep_bonus
    if is_open:
        return (2, round(min(99.0, max(92.0, heat)), 0), "⚡ IN-TRADE")
    if was_just_closed:
        return (0, round(min(75.0, heat * 0.5), 0), "⏳ COOLDOWN")
    badge = "🔥 BREAKOUT" if (choch != "NONE" or sweep_bonus > 0) else ("🎯 HOT MOVE" if heat >= 52 else "📡 ACTIVE MOVE")
    return (1, round(min(98.0, max(65.0, heat)), 0), badge)

def _enrich_remote_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    mkts = data.get("markets", {})
    if len(mkts) < 30:
        try:
            r = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr", timeout=4)
            b_map = {it["symbol"]: it for it in r.json()} if r.status_code == 200 else {}
        except Exception:
            b_map = {}
        for sym in config.SYMBOLS:
            if sym not in mkts:
                params = config.get_asset_params(sym)
                b_it = b_map.get(params["bin_symbol"], {})
                p_cur = float(b_it.get("lastPrice") or params["default_price"])
                pdh = float(b_it.get("highPrice") or p_cur * 1.02)
                pdl = float(b_it.get("lowPrice") or p_cur * 0.98)
                chg = float(b_it.get("priceChangePercent") or 0.0)
                rel_pos = (p_cur - pdl) / max(1e-6, pdh - pdl) if p_cur > 0 else 0.5
                mkts[sym] = {
                    "symbol": sym, "display_name": params["display_name"], "price": p_cur,
                    "session": data.get("session", "ACTIVE"), "pdh": pdh, "pdl": pdl,
                    "macro_bias": "BULLISH" if chg > 0.6 else ("BEARISH" if chg < -0.6 else "NEUTRAL"),
                    "adx": round(min(45.0, 18.0 + abs(chg) * 2.2), 1),
                    "rsi": round(max(25.0, min(75.0, 50.0 + chg * 2.5)), 1),
                    "cvd_pct": round(max(30.0, min(70.0, 50.0 + chg * 1.8)), 1),
                    "zone_name": "DISCOUNT" if rel_pos < 0.45 else ("PREMIUM" if rel_pos > 0.55 else "EQUILIBRIUM"),
                }

    for sym, m in mkts.items():
        p_cur = float(m.get("price") or 0.0)
        pdh, pdl = float(m.get("pdh") or p_cur * 1.015), float(m.get("pdl") or p_cur * 0.985)
        if not m.get("zone_name"):
            rel_pos = (p_cur - pdl) / max(1e-6, pdh - pdl) if p_cur > 0 else 0.5
            m["zone_name"] = "DISCOUNT" if rel_pos < 0.45 else ("PREMIUM" if rel_pos > 0.55 else "EQUILIBRIUM")
        m["adx"] = float(m.get("adx") or 24.5)
        m["rsi"] = float(m.get("rsi") or 50.0)
        m["cvd_pct"] = float(m.get("cvd_pct") or 50.0)
    vault = data.get("vault", [])
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data.setdefault("day_trades", sum(1 for t in vault if str(t.get("opened_at_utc", "")).startswith(today_prefix)))
    data.setdefault("day_pnl_usd", round(sum(float(t.get("realized_pnl_usd") or 0.0) for t in vault if str(t.get("closed_at_utc", "")).startswith(today_prefix)), 2))
    if "compounding" not in data:
        net_pnl = float(data.get("realized_pnl_usd") or 0.0)
        tot_fee = round(sum(float(t.get("total_fee_usd") or 0.0) for t in vault), 2)
        w_usd = round(max(0.0, net_pnl * 0.30), 2)
        r_usd = round(net_pnl * 0.70 if net_pnl > 0 else net_pnl, 2)
        act_eq = round(max(6.0, float(config.START_EQUITY) + r_usd), 2)
        data["compounding"] = {
            "base_capital_usd": float(config.START_EQUITY),
            "cycle_15d_net_pnl_usd": round(net_pnl, 2), "total_gross_pnl_usd": round(net_pnl + tot_fee, 2),
            "total_brokerage_usd": tot_fee, "withdrawable_usd": w_usd, "active_trading_equity_usd": act_eq,
            "lot_multiplier": round(max(0.75, min(10.0, act_eq / 58.0)), 2),
            "scaling_status": "SCALING UP (PROFIT)" if net_pnl > 0 else ("RECOVERY MODE (TIGHT SL)" if net_pnl < 0 else "BASE LOT SIZE"),
        }
    return data

def _run_local_step():
    is_leader, token = database.lease_tick()
    market_data.refresh_all_market_data(_LOCAL_LOCK)
    if is_leader and token is not None:
        trade_manager.manage_all_positions(token, _LOCAL_LOCK)
    auto_enabled = database.get_setting("auto_trade_enabled", "1") == "1"
    for sym in list(config.SYMBOLS):
        with _LOCAL_LOCK:
            m = dict(market_data.get_market(sym))
        st_info = smc_engine.get_structure(m, sym)
        if st_info:
            with _LOCAL_LOCK:
                _LOCAL_STRUCT[sym] = st_info
            if is_leader and token is not None and auto_enabled:
                sig = smc_engine.evaluate_signal(st_info, m.get("klines", {}).get("15m", []), m.get("price"), m.get("session"), m.get("poc"), sym)
                if sig:
                    trade_manager.open_paper_trade(sig, token)

def _bg_worker_loop():
    while True:
        try:
            _run_local_step()
        except Exception:
            pass
        time.sleep(12)

@st.cache_resource
def _start_background_engine():
    database.init_db()
    _run_local_step()
    threading.Thread(target=_bg_worker_loop, daemon=True).start()
    return True

def _build_local_payload() -> Dict[str, Any]:
    _start_background_engine()
    c = database.db()
    try:
        ts = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()]
        active_rows = [dict(r) for r in c.execute("SELECT * FROM trades WHERE stage != 'CLOSED' ORDER BY id DESC LIMIT 10").fetchall()]
        pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()["p"] or 0.0)
        unrealized = float(c.execute("SELECT COALESCE(SUM(unrealized_pnl_usd),0) u FROM trades WHERE stage != 'CLOSED'").fetchone()["u"] or 0.0)
        compounding = trade_manager.get_15d_compounding_state(c)
        base_cap = compounding["base_capital_usd"]
        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
        day_trades = int(c.execute("SELECT COUNT(*) as c FROM trades WHERE opened_at_utc >= ?", (today_start,)).fetchone()["c"] or 0)
        day_pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0.0) as s FROM trades WHERE closed_at_utc >= ?", (today_start,)).fetchone()["s"] or 0.0)
        closed_asc = c.execute("SELECT symbol, realized_pnl_usd, closed_at_utc FROM trades WHERE stage = 'CLOSED' OR realized_pnl_usd != 0 ORDER BY id ASC").fetchall()
        wins = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) > 0]
        losses = [float(r["realized_pnl_usd"] or 0.0) for r in closed_asc if float(r["realized_pnl_usd"] or 0.0) < 0]
        tot = len(wins) + len(losses)
        win_rate = round((len(wins) / tot) * 100.0, 1) if tot > 0 else 0.0
        gw, gl = sum(wins), abs(sum(losses))
        pf = round(gw / gl, 2) if gl > 0 else (round(gw, 2) if gw > 0 else 0.0)
        eq, peak, max_dd = float(base_cap), float(base_cap), 0.0
        equity_curve = [{"trade": 0, "equity": round(eq, 2), "drawdown": 0.0}]
        for idx, r in enumerate(closed_asc, start=1):
            eq += float(r["realized_pnl_usd"] or 0.0)
            peak = max(peak, eq)
            dd = round(((peak - eq) / peak) * 100.0, 2) if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            equity_curve.append({"trade": idx, "equity": round(eq, 2), "drawdown": -dd})
        markets_info = {}
        for sym in list(config.SYMBOLS):
            with _LOCAL_LOCK:
                m, st_info = dict(market_data.get_market(sym)), dict(_LOCAL_STRUCT.get(sym, {}))
            params = config.get_asset_params(sym, m.get("price"))
            p_cur = float(m.get("price") or params["default_price"])
            pdh, pdl = float(m.get("pdh") or m.get("day_high_24h") or p_cur * 1.015), float(m.get("pdl") or m.get("day_low_24h") or p_cur * 0.985)
            rel_pos = (p_cur - pdl) / max(1e-6, pdh - pdl) if p_cur > 0 else 0.5
            markets_info[sym] = {
                "symbol": sym, "display_name": params["display_name"], "price": p_cur, "session": m.get("session", "ACTIVE"),
                "pdh": pdh, "pdl": pdl, "macro_bias": st_info.get("macro_bias", "NEUTRAL"),
                "adx": round(float(st_info.get("adx") or 24.0), 1), "rsi": round(float(st_info.get("rsi") or 50.0), 1),
                "cvd_pct": round(float(m.get("cvd_ratio") or 0.5) * 100.0, 1),
                "choch_status": st_info.get("choch_status", "NONE"),
                "zone_name": "DISCOUNT" if rel_pos < 0.45 else ("PREMIUM" if rel_pos > 0.55 else "EQUILIBRIUM"),
            }
        btc_m = market_data.get_market("BTCUSDT")
        bal = round(base_cap + pnl, 2)
        return {
            "mode": config.MODE, "auto_trade_enabled": database.get_setting("auto_trade_enabled", "1") == "1",
            "symbols": config.SYMBOLS, "markets": markets_info, "compounding": compounding,
            "wallet": {"balance_usd": bal, "available_usd": bal, "is_live_synced": False},
            "session": btc_m.get("session", "ACTIVE"), "killzone_name": btc_m.get("killzone_name", "OFF_HOURS"),
            "is_dead_hour": btc_m.get("is_dead_hour", False), "news_blackout": btc_m.get("news_blackout", False),
            "realized_pnl_usd": round(pnl, 2), "unrealized_pnl_usd": round(unrealized, 2),
            "day_trades": day_trades, "day_pnl_usd": round(day_pnl, 2),
            "stats": {"wins": len(wins), "losses": len(losses), "total_trades": tot, "win_rate": win_rate, "profit_factor": pf, "max_drawdown_pct": round(max_dd, 2), "expectancy_usd": round(pnl / tot, 2) if tot > 0 else 0.0},
            "equity_curve": equity_curve, "vault": ts, "active": active_rows,
        }
    finally:
        c.close()

def fetch_vault_payload() -> Dict[str, Any]:
    global _LAST_FETCH_TS, _CACHED_PAYLOAD
    now = time.time()
    if _CACHED_PAYLOAD and (now - _LAST_FETCH_TS) < 4.0:
        return _CACHED_PAYLOAD
    for base_url in _get_backend_candidates():
        try:
            resp = requests.get(f"{base_url}/vault-data", timeout=5)
            if resp.status_code == 200:
                _CACHED_PAYLOAD = _enrich_remote_payload(resp.json())
                _LAST_FETCH_TS = now
                return _CACHED_PAYLOAD
        except Exception:
            continue
    _CACHED_PAYLOAD = _build_local_payload()
    _LAST_FETCH_TS = now
    return _CACHED_PAYLOAD

def toggle_bot_state() -> None:
    global _LAST_FETCH_TS
    _LAST_FETCH_TS = 0.0
    for base_url in _get_backend_candidates():
        try:
            if requests.post(f"{base_url}/toggle-bot", timeout=4).status_code == 200:
                return
        except Exception:
            continue
    cur = database.get_setting("auto_trade_enabled", "1")
    database.set_setting("auto_trade_enabled", "0" if cur == "1" else "1")

def add_capital_step(amount_usd: float = 58.0) -> None:
    global _LAST_FETCH_TS
    _LAST_FETCH_TS = 0.0
    for base_url in _get_backend_candidates():
        try:
            if requests.post(f"{base_url}/add-capital", json={"amount_usd": amount_usd}, timeout=4).status_code == 200:
                return
        except Exception:
            continue
    cur_added = float(database.get_setting("added_capital_usd", "0.0") or 0.0)
    database.set_setting("added_capital_usd", str(max(0.0, round(cur_added + amount_usd, 2))))

st.markdown("""<style>
[data-stale="true"], .stApp [data-stale="true"], div[data-testid="stAppViewBlockContainer"] * { opacity: 1 !important; transition: none !important; filter: none !important; }
div[data-testid="stStatusWidget"] { display: none !important; }
body, .stApp { background-color: #040711; color: #f8fafc; font-family: 'Inter', sans-serif; }
.block-container { padding-top: 3.6rem !important; padding-bottom: 2.2rem !important; max-width: 1180px; }
.top-bar { background: linear-gradient(135deg, #0f172a 0%, #090d16 100%); border: 1px solid #1e293b; border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.brand-title { font-size: 1.05rem; font-weight: 800; letter-spacing: 0.05em; color: #f8fafc; font-family: 'JetBrains Mono', monospace; }
.brand-sub { font-size: 0.72rem; color: #94a3b8; margin-top: 3px; }
.status-pill { padding: 5px 11px; border-radius: 6px; font-size: 0.70rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.kpi-grid, .market-grid, .guard-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 14px; }
@media (min-width: 768px) { .kpi-grid, .guard-grid { grid-template-columns: repeat(4, 1fr); } }
@media (min-width: 900px) { .market-grid { grid-template-columns: repeat(4, 1fr); } }
.kpi-box, .mkt-card { background: #0a0f1d; border: 1px solid #1e293b; border-radius: 10px; padding: 12px 14px; }
.kpi-title { color: #94a3b8; font-size: 0.66rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
.kpi-val { font-size: 1.25rem; font-weight: 800; font-family: 'JetBrains Mono', monospace; color: #38bdf8; }
.kpi-sub { color: #64748b; font-size: 0.70rem; margin-top: 3px; }
.sec-title { font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: #cbd5e1; margin: 14px 0 8px 0; }
.mkt-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 3px; }
.mkt-name { font-size: 0.74rem; font-weight: 700; color: #e2e8f0; }
.mkt-price { font-size: 1.18rem; font-weight: 800; font-family: 'JetBrains Mono', monospace; margin: 3px 0 6px 0; }
.tag-bull { background: rgba(16,185,129,0.14); color: #10b981; border: 1px solid rgba(16,185,129,0.4); padding: 2px 6px; border-radius: 5px; font-size: 0.62rem; font-weight: 700; }
.tag-bear { background: rgba(239,68,68,0.14); color: #ef4444; border: 1px solid rgba(239,68,68,0.4); padding: 2px 6px; border-radius: 5px; font-size: 0.62rem; font-weight: 700; }
.tag-neut { background: rgba(245,158,11,0.14); color: #f59e0b; border: 1px solid rgba(245,158,11,0.4); padding: 2px 6px; border-radius: 5px; font-size: 0.62rem; font-weight: 700; }
.mkt-row { display: flex; justify-content: space-between; font-size: 0.68rem; color: #94a3b8; border-top: 1px solid #131c31; padding-top: 4px; margin-top: 4px; }
.guard-box { background: #080d18; border: 1px solid #172238; border-radius: 8px; padding: 9px 11px; font-size: 0.70rem; color: #94a3b8; }
.pos-card { background: #0d1527; border-left: 4px solid #38bdf8; border-radius: 8px; padding: 11px 13px; margin-bottom: 8px; border-top: 1px solid #1e293b; border-right: 1px solid #1e293b; border-bottom: 1px solid #1e293b; }
</style>""", unsafe_allow_html=True)

def _render_dashboard_body():
    payload = fetch_vault_payload()
    mode, auto_on = payload.get("mode", "PAPER"), payload.get("auto_trade_enabled", True)
    wallet = payload.get("wallet", {})
    bal_usd = float(wallet.get("balance_usd") or config.START_EQUITY)
    avail_usd = float(wallet.get("available_usd") or bal_usd)
    used_margin = max(0.0, round(bal_usd - avail_usd, 2))
    realized_pnl, unrealized_pnl = float(payload.get("realized_pnl_usd", 0.0)), float(payload.get("unrealized_pnl_usd", 0.0))
    day_trades, day_pnl = int(payload.get("day_trades", 0)), float(payload.get("day_pnl_usd", 0.0))
    stats, comp = payload.get("stats", {}), payload.get("compounding", {})
    markets, active_list, vault_trades, eq_curve = payload.get("markets", {}), payload.get("active", []), payload.get("vault", []), payload.get("equity_curve", [])

    s_bg = "rgba(16,185,129,0.14)" if auto_on else "rgba(239,68,68,0.14)"
    s_bd = "#10b981" if auto_on else "#ef4444"
    s_tx = f"● {mode} ENGINE ONLINE" if auto_on else "⏸ ENGINE PAUSED"
    st.markdown(f'<div class="top-bar"><div><div class="brand-title">⚡ SR QUANTX — ALGO TERMINAL</div><div class="brand-sub">{payload.get("session", "ACTIVE")} • {payload.get("killzone_name", "OFF_HOURS").replace("_", " ")} • 25x Leverage • Background Radar: 120+ Delta Assets ({len(markets)} Deep-Scanned)</div></div><div class="status-pill" style="background:{s_bg};border:1px solid {s_bd};color:{s_bd};">{s_tx}</div></div>', unsafe_allow_html=True)

    pnl_clr, upnl_clr = ("#10b981" if realized_pnl >= 0 else "#ef4444"), ("#10b981" if unrealized_pnl >= 0 else "#ef4444")
    st.markdown(f'<div class="kpi-grid"><div class="kpi-box"><div class="kpi-title">PORTFOLIO EQUITY</div><div class="kpi-val">${bal_usd:,.2f}</div><div class="kpi-sub">₹{bal_usd*USD_INR:,.0f} • Free: ${avail_usd:,.2f}</div></div><div class="kpi-box"><div class="kpi-title">NET REALIZED PNL</div><div class="kpi-val" style="color:{pnl_clr};">${realized_pnl:+.2f}</div><div class="kpi-sub">₹{realized_pnl*USD_INR:+,.0f} • Expectancy: ${stats.get("expectancy_usd",0.0):+.2f}</div></div><div class="kpi-box"><div class="kpi-title">OPEN FLOATING PNL</div><div class="kpi-val" style="color:{upnl_clr};">${unrealized_pnl:+.2f}</div><div class="kpi-sub">Active: {len(active_list)}/{config.MAX_OPEN_TRADES} • Margin: ${used_margin:.2f}</div></div><div class="kpi-box"><div class="kpi-title">WIN RATE & PROFIT FACTOR</div><div class="kpi-val">{stats.get("win_rate",0.0):.1f}%</div><div class="kpi-sub">{stats.get("wins",0)}W / {stats.get("losses",0)}L • PF: {stats.get("profit_factor",0.0):.2f} • DD: -{stats.get("max_drawdown_pct",0.0):.1f}%</div></div></div>', unsafe_allow_html=True)

    c_15d = float(comp.get("cycle_15d_net_pnl_usd", realized_pnl))
    c_gross = float(comp.get("total_gross_pnl_usd", realized_pnl))
    c_fees = float(comp.get("total_brokerage_usd", 0.0))
    c_with = float(comp.get("withdrawable_usd", max(0.0, realized_pnl * 0.30)))
    c_act_eq = float(comp.get("active_trading_equity_usd", bal_usd))
    c_mult = float(comp.get("lot_multiplier", 1.0))
    c_stat = comp.get("scaling_status", "BASE LOT SIZE")
    c_clr = "#10b981" if c_15d >= 0 else "#ef4444"

    st.markdown('<div class="sec-title">🏛️ 15-Day Compounding, Lot Scaling & Withdrawal Vault</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="kpi-grid"><div class="kpi-box"><div class="kpi-title">15-DAY NET PNL (AFTER FEES)</div><div class="kpi-val" style="color:{c_clr};">${c_15d:+.2f}</div><div class="kpi-sub">₹{c_15d*USD_INR:+,.0f} • Gross: ${c_gross:+.2f}</div></div><div class="kpi-box"><div class="kpi-title">FREE FOR WITHDRAWAL (30%)</div><div class="kpi-val" style="color:#10b981;">${c_with:.2f}</div><div class="kpi-sub">₹{c_with*USD_INR:,.0f} Safe to Withdraw</div></div><div class="kpi-box"><div class="kpi-title">COMPOUNDED TRADING CAP (70%)</div><div class="kpi-val">${c_act_eq:.2f}</div><div class="kpi-sub">Lot Scale: {c_mult:.2f}x ({c_stat})</div></div><div class="kpi-box"><div class="kpi-title">TOTAL BROKERAGE DEDUCTED</div><div class="kpi-val" style="color:#f59e0b;">-${c_fees:.2f}</div><div class="kpi-sub">-₹{c_fees*USD_INR:,.0f} (Delta Fee + GST Deducted)</div></div></div>', unsafe_allow_html=True)

    dead_ok = "BLOCKED" if payload.get("is_dead_hour") else "ACTIVE"
    news_ok = "BLACKOUT" if payload.get("news_blackout") else "CLEAR"
    st.markdown(f'<div class="guard-grid"><div class="guard-box"><b>DAILY TRADES:</b> <span style="color:#f8fafc;">{day_trades} / {config.MAX_DAILY_TRADES}</span></div><div class="guard-box"><b>DAILY LOSS LIMIT:</b> <span style="color:#f8fafc;">${day_pnl:+.2f} / -${config.MAX_DAILY_LOSS * max(1.0, c_mult):.2f}</span></div><div class="guard-box"><b>ACTIVE LOT RISK:</b> <span style="color:#38bdf8;">{config.RISK_PCT*100:.1f}% (~${c_act_eq*config.RISK_PCT:.2f})</span></div><div class="guard-box"><b>LIQUIDITY / NEWS:</b> <span style="color:#10b981;">{dead_ok} / {news_ok}</span></div></div>', unsafe_allow_html=True)

    disp_n = int(getattr(config, "DISPLAY_SCANNER_CARDS", 8))
    st.markdown(f'<div class="sec-title">🔥 Live Spotlight Radar (Top {disp_n} Active Movers & Open Trades — Auto-Rotating from {len(markets)}+ Scanned Assets)</div>', unsafe_allow_html=True)
    if markets:
        cards_html = '<div class="market-grid">'
        active_syms = {t.get("symbol") for t in active_list if t.get("symbol")}
        recent_closed_syms = {t.get("symbol") for t in vault_trades[:6] if t.get("stage") == "CLOSED" and t.get("symbol")}

        scored_mkts = []
        for sym, m in markets.items():
            tier, heat, badge = _calc_mover_heat_score(sym, m, active_syms, recent_closed_syms)
            scored_mkts.append((tier, heat, badge, sym, m))
        scored_mkts.sort(key=lambda x: (x[0], x[1]), reverse=True)

        for tier, heat, badge, sym, m in scored_mkts[:disp_n]:
            p, bias, adx, cvd, rsi = float(m.get("price") or 0.0), m.get("macro_bias", "NEUTRAL"), float(m.get("adx") or 22.0), float(m.get("cvd_pct") or 50.0), float(m.get("rsi") or 50.0)
            pdh, pdl, zone = float(m.get("pdh") or 0.0), float(m.get("pdl") or 0.0), m.get("zone_name", "EQUILIBRIUM")
            t_cls, t_txt = ("tag-bull", "▲ BULLISH") if bias == "BULLISH" else (("tag-bear", "▼ BEARISH") if bias == "BEARISH" else ("tag-neut", "• NEUTRAL"))
            p_clr = "#fbbf24" if "PAXG" in sym else ("#38bdf8" if tier == 2 else "#f8fafc")
            card_border = "border: 1px solid #38bdf8;" if tier == 2 else ""
            short_name = m.get("display_name", sym).split("(")[0].strip()
            flow_txt = f"Buy {cvd:.0f}%" if cvd >= 50 else f"Sell {100-cvd:.0f}%"
            flow_clr = "#10b981" if cvd >= 52 else ("#ef4444" if cvd <= 48 else "#94a3b8")
            cards_html += f'<div class="mkt-card" style="{card_border}"><div class="mkt-head"><span class="mkt-name">{short_name}</span><span class="{t_cls}">{t_txt}</span></div><div class="mkt-price" style="color:{p_clr};">{_fmt_p(p)}</div><div class="mkt-row"><span>Radar Status:</span><b style="color:#38bdf8;">{badge} ({heat:.0f}%)</b></div><div class="mkt-row"><span>SMC / ADX / RSI:</span><b style="color:#e2e8f0;">{zone[:4]} • {adx:.0f} / {rsi:.0f}</b></div><div class="mkt-row"><span>CVD Delta:</span><b style="color:{flow_clr};">{flow_txt}</b></div><div class="mkt-row"><span>PDH / PDL:</span><b>{_fmt_p(pdh)} / {_fmt_p(pdl)}</b></div></div>'
        cards_html += '</div>'
        st.markdown(cards_html, unsafe_allow_html=True)

    st.markdown(f'<div class="sec-title">⚡ Open Positions ({len(active_list)}/{config.MAX_OPEN_TRADES})</div>', unsafe_allow_html=True)
    if active_list:
        for tr in active_list:
            upnl = float(tr.get("unrealized_pnl_usd") or 0.0)
            rpnl = float(tr.get("realized_pnl_usd") or 0.0)
            u_clr, side = ("#10b981" if upnl >= 0 else "#ef4444"), tr.get("direction", "LONG")
            b_clr = "#10b981" if side == "LONG" else "#ef4444"
            booked_tag = f" • Booked TP1: ${rpnl:+.2f}" if rpnl != 0 else ""
            st.markdown(f'<div class="pos-card" style="border-left-color:{b_clr};"><div style="display:flex;justify-content:space-between;align-items:center;"><b style="font-size:0.88rem;">{tr.get("symbol")} • {side} ({tr.get("setup_name")}){booked_tag}</b><span style="color:{u_clr};font-weight:800;font-size:0.92rem;">${upnl:+.2f} (₹{upnl*USD_INR:+,.0f})</span></div><div style="font-size:0.74rem;color:#94a3b8;margin-top:5px;">Entry: <b>{_fmt_p(float(tr["entry_price"]))}</b> | SL: <b style="color:#ef4444;">{_fmt_p(float(tr["stop_price"]))}</b> | TP1 (1.30R): <b style="color:#10b981;">{_fmt_p(float(tr["tp1_price"]))}</b> | TP2 (2.40R): <b style="color:#10b981;">{_fmt_p(float(tr["tp2_price"]))}</b></div></div>', unsafe_allow_html=True)
    else:
        st.caption(f"No open positions — Background Radar sweeping 120+ Delta assets ({len(markets)} deep-scanned) & auto-promoting top movers above.")

    tab1, tab2, tab3 = st.tabs(["📜 Execution Log (With Brokerage)", "📈 Equity Curve", "📊 Strategy & Risk Specs"])
    with tab1:
        if vault_trades:
            df_log = pd.DataFrame([{
                "Time (UTC)": str(t.get("opened_at_utc", ""))[11:19], "Asset": str(t.get("symbol", "")).replace("USDT", ""),
                "Side": t.get("direction"), "Strategy": t.get("setup_name"), "Entry": _fmt_p(float(t.get("entry_price") or 0)),
                "SL": _fmt_p(float(t.get("stop_price") or 0)), "Exit Reason": t.get("close_reason") or ("TP1_BOOKED_RUNNING" if t.get("tp1_done") else "ACTIVE"),
                "Gross ($)": f"${(float(t.get('realized_pnl_usd') or 0.0) + float(t.get('total_fee_usd') or 0.0)):+.2f}",
                "Fee ($)": f"-${float(t.get('total_fee_usd') or 0.0):.2f}", "Net PnL ($)": f"${float(t.get('realized_pnl_usd') or 0.0):+.2f}",
            } for t in vault_trades])
            st.dataframe(df_log, use_container_width=True, hide_index=True)
        else:
            st.caption("No historical trades recorded yet.")
    with tab2:
        if len(eq_curve) > 1:
            st.line_chart(pd.DataFrame(eq_curve).set_index("trade")[["equity"]], height=210)
        else:
            st.caption("Equity curve will render automatically after the first closed trade.")
    with tab3:
        st.markdown(f"- **Dynamic Spotlight Radar:** Sweeps 120+ Delta perpetuals in background, displays only the **Top 8 Active Movers & Open Trades**, and automatically rotates out closed trades for fresh movers\n- **Proportional Capital Scaling:** Base `₹5,000 ($58)` scales lot sizes & profit targets automatically whenever you add `₹5,000 (+$58)` (`{c_mult:.2f}x` active multiplier)\n- **Fast Realized Profit Lock:** `+0.80R` slashes risk in half | `TP1 (+1.30R)` books `35%` & locks `65%` risk-free at BE+fees | `TP2 (+2.40R)` books `35%` & trails SL to `+1.35R` | `TP3 (+3.80R)` rides `30%` mega-runner")

def main() -> None:
    if hasattr(st, "fragment"):
        @st.fragment(run_every="15s")
        def _live_fragment():
            _render_dashboard_body()
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("⏯ Pause / Resume Engine", use_container_width=True):
                    toggle_bot_state()
                    st.rerun()
            with c2:
                if st.button("➕ Add ₹5,000 ($58) Capital", use_container_width=True):
                    add_capital_step(58.0)
                    st.rerun()
            with c3:
                if st.button("🔄 Force Sync Now", use_container_width=True):
                    global _LAST_FETCH_TS
                    _LAST_FETCH_TS = 0.0
                    st.rerun()
        _live_fragment()
    else:
        _render_dashboard_body()

if __name__ == "__main__":
    main()
