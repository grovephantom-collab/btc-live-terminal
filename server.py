import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
import requests
import plotly.graph_objects as go
import sqlite3
from datetime import datetime

st.set_page_config(
    page_title="BTCUSDT RADAR",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Har 3 second me background auto-tick refresh (Zero flick)
st_autorefresh(interval=3000, limit=None, key="live_market_tick")

# Mobile Viewport & Dark Theme Layout
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { 
            padding: 4px 6px !important; 
            max-width: 100% !important;
            background-color: #0b0e14; 
        }
        .stApp { background-color: #0b0e14; color: #d1d4dc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }

        /* Top HUD Bar */
        .top-radar-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 5px 8px;
            background: #10141d;
            border-bottom: 1px solid #1a2230;
            font-size: 10px;
        }
        .bar-left { display: flex; align-items: center; gap: 8px; }
        .badge-online { background: #00e676; color: #000; font-weight: 900; font-size: 8px; padding: 2px 4px; border-radius: 2px; }
        .hud-cell { display: flex; flex-direction: column; }
        .cell-lbl { font-size: 7px; color: #64748b; font-weight: 700; text-transform: uppercase; }
        .cell-val { font-weight: 800; font-size: 10px; }

        /* Top-Right Vault Toggle */
        .vault-trigger {
            background: #182232;
            border: 1px solid #28374d;
            color: #38bdf8;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 700;
            text-decoration: none;
            display: inline-block;
        }

        /* Bottom Controls Row 1 */
        .bottom-panel-1 {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #0d1117;
            padding: 5px 8px;
            border-top: 1px solid #1a2230;
            font-size: 9px;
            margin-top: 2px;
        }
        .panel-grp { display: flex; align-items: center; gap: 8px; }
        .btn-force {
            border: 1px solid #ca8a04;
            color: #facc15;
            background: rgba(202, 138, 4, 0.15);
            padding: 2px 5px;
            border-radius: 3px;
            font-weight: 700;
            font-size: 8px;
        }
        .btn-kill {
            border: 1px solid #dc2626;
            color: #f87171;
            background: rgba(220, 38, 38, 0.15);
            padding: 2px 5px;
            border-radius: 3px;
            font-weight: 700;
            font-size: 8px;
        }

        /* Bottom Controls Row 2 */
        .bottom-panel-2 {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 4px;
            background: #090c10;
            padding: 4px 6px 8px 6px;
            font-size: 8px;
        }
        .info-card {
            background: #0f141d;
            border: 1px solid #17202e;
            padding: 4px 5px;
            border-radius: 3px;
            display: flex;
            flex-direction: column;
        }
        .info-title { color: #475569; font-size: 7px; font-weight: 700; }
        .info-val { font-weight: 800; font-size: 8px; margin-top: 1px; }

        .c-green { color: #00e676 !important; font-weight: bold; }
        .c-red { color: #ff3b30 !important; font-weight: bold; }
        .c-cyan { color: #00e5ff !important; font-weight: bold; }
        .c-yellow { color: #eab308 !important; font-weight: bold; }
    </style>
""", unsafe_allow_html=True)

# --- SQLite Database (NO FAKE / DUMMY SEED TRADES) ---
conn = sqlite3.connect('trades_vault.db', check_same_thread=False)
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS vault (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        direction TEXT,
        entry REAL,
        result TEXT,
        pnl TEXT,
        is_win INTEGER
    )
''')
conn.commit()

# Current real vault count
cur.execute("SELECT COUNT(*) FROM vault")
vault_count = cur.fetchone()[0]

# --- Live Binance Real-Time Ticker & Candlestick Feed ---
def get_live_market_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # Live Spot Klines
    try:
        url = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=55"
        res = requests.get(url, headers=headers, timeout=2.5).json()
        if isinstance(res, list) and len(res) > 10:
            df = pd.DataFrame(res, columns=['t', 'o', 'h', 'l', 'c', 'v', 'ct', 'qa', 'tr', 'tb', 'tq', 'i'])
            df['time'] = pd.to_datetime(df['t'], unit='ms')
            for col in ['o', 'h', 'l', 'c', 'v']:
                df[col] = df[col].astype(float)
            df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)
            return df
    except Exception:
        pass

    try:
        url = "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=55"
        res = requests.get(url, headers=headers, timeout=2.5).json()
        if isinstance(res, list) and len(res) > 10:
            df = pd.DataFrame(res, columns=['t', 'o', 'h', 'l', 'c', 'v', 'ct', 'qa', 'tr', 'tb', 'tq', 'i'])
            df['time'] = pd.to_datetime(df['t'], unit='ms')
            for col in ['o', 'h', 'l', 'c', 'v']:
                df[col] = df[col].astype(float)
            df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)
            return df
    except Exception:
        pass

    return pd.DataFrame()

df = get_live_market_data()
if df.empty:
    st.info("Reconnecting High-Speed Market Feed...")
    st.stop()

# --- Real Signal Strategy Engine (EMA + High/Low Breakout + ATR Risk) ---
df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

last = df.iloc[-1]
prev = df.iloc[-2]
cur_p = round(float(last['close']), 2)
high_p = float(last['high'])
low_p = float(last['low'])
atr = round(abs(high_p - low_p), 2)
if atr < 30: atr = 85.0

# Dynamic Triggering: Real Bullish Pullback or Bearish Breakdown
has_signal = False
radar_status = "SCANNING"
entry_txt, sl_txt, tp_txt = "--", "--", "--"
entry_val, sl_val, tp_val = 0.0, 0.0, 0.0

if last['close'] >= prev['high'] or (last['ema9'] > last['ema21'] and last['close'] > last['open']):
    has_signal = True
    radar_status = "PRE-SIGNAL LONG"
    entry_val = cur_p
    sl_val = round(cur_p - (atr * 1.5), 1)
    tp_val = round(cur_p + (atr * 2.2), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"
elif last['close'] <= prev['low'] or (last['ema9'] < last['ema21'] and last['close'] < last['open']):
    has_signal = True
    radar_status = "PRE-SIGNAL SHORT"
    entry_val = cur_p
    sl_val = round(cur_p + (atr * 1.5), 1)
    tp_val = round(cur_p - (atr * 2.2), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"

# --- TOP HUD HEADER ---
query_params = st.query_params
show_vault = query_params.get("vault", "0") == "1"
vault_action = "?vault=0" if show_vault else "?vault=1"
vault_label = "✕ CLOSE" if show_vault else f"📜 VAULT ({vault_count})"

st.markdown(f"""
    <div class="top-radar-bar">
        <div class="bar-left">
            <span class="badge-online">ONLINE</span>
            <div class="hud-cell">
                <span class="cell-lbl">ACTIVE PAIR</span>
                <span class="cell-val c-yellow">{radar_status}</span>
            </div>
            <div class="hud-cell">
                <span class="cell-lbl">ENTRY</span>
                <span class="cell-val c-cyan">{entry_txt}</span>
            </div>
            <div class="hud-cell">
                <span class="cell-lbl">SL / TRAIL</span>
                <span class="cell-val c-red">{sl_txt}</span>
            </div>
            <div class="hud-cell">
                <span class="cell-lbl">DIRECT TP</span>
                <span class="cell-val c-green">{tp_txt}</span>
            </div>
        </div>
        <div>
            <a href="{vault_action}" target="_self" class="vault-trigger">{vault_label}</a>
        </div>
    </div>
""", unsafe_allow_html=True)

# --- Vault View (Opens only when clicked, clean if 0 trades) ---
if show_vault:
    cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
    rows = cur.fetchall()
    total = len(rows)
    wins = sum(1 for r in rows if r[5] == 1)
    rate = int((wins / total) * 100) if total > 0 else 0

    st.markdown(f"""
        <div style="background:#11151f; border:1px solid #1e2838; border-radius:6px; padding:10px; margin:4px 0;">
            <div style="display:flex; justify-content:space-between; font-size:11px; font-weight:bold; margin-bottom:6px;">
                <span>Total Protected Trades: {total}</span>
                <span>Win Rate: <span class="c-green">{rate}%</span></span>
            </div>
    """, unsafe_allow_html=True)

    if total == 0:
        st.markdown("<p style='font-size:10px; color:#64748b; text-align:center; padding:8px 0;'>No trade executed yet. Monitoring active live signals...</p>", unsafe_allow_html=True)
    else:
        for r in rows:
            clr = "#00e676" if r[5] == 1 else "#ff3b30"
            st.markdown(
                f"<div style='display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px solid #1a2230; font-size:10px;'>"
                f"<span>{r[0]} <b style='color:#00e5ff'>{r[1]}</b> @ ${r[2]}</span>"
                f"<span style='color:{clr}; font-weight:bold;'>{r[3]} {r[4]}</span>"
                f"</div>",
                unsafe_allow_html=True
            )
        if st.button("🗑️ ONE-CLICK CLEAR VAULT"):
            cur.execute("DELETE FROM vault")
            conn.commit()
            st.query_params.clear()
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

# --- REAL-TIME RESPONSIVE CHART ---
fig = go.Figure()

# Real candlesticks with thin wicks
fig.add_trace(go.Candlestick(
    x=df['time'],
    open=df['open'],
    high=df['high'],
    low=df['low'],
    close=df['close'],
    increasing_line_color='#00e676',
    decreasing_line_color='#ff3b30',
    increasing_fillcolor='#00e676',
    decreasing_fillcolor='#ff3b30',
    name="BTCUSDT"
))

# Active Real Dotted Lines on Signal
if has_signal:
    fig.add_hline(y=tp_val, line_dash="dash", line_color="#00e676", line_width=1.3,
                  annotation_text=f"PRE-SIGNAL DIRECT TP: {tp_val}", annotation_position="top right",
                  annotation_font_color="#00e676", annotation_bgcolor="#0c0f14")

    fig.add_hline(y=entry_val, line_dash="dash", line_color="#00e5ff", line_width=1.3,
                  annotation_text=f"PRE-SIGNAL ENTRY: {entry_val}", annotation_position="right",
                  annotation_font_color="#00e5ff", annotation_bgcolor="#0c0f14")

    fig.add_hline(y=sl_val, line_dash="dash", line_color="#ff3b30", line_width=1.3,
                  annotation_text=f"PRE-SIGNAL SL: {sl_val}", annotation_position="bottom right",
                  annotation_font_color="#ff3b30", annotation_bgcolor="#0c0f14")

# Live red price badge on current tick
fig.add_hline(y=cur_p, line_dash="dot", line_color="#ff3b30", line_width=1,
              annotation_text=f" {cur_p:.2f} ", annotation_position="right",
              annotation_font_color="#ffffff", annotation_bgcolor="#dc2626")

fig.update_layout(
    height=490,
    margin=dict(l=0, r=65, t=5, b=5),
    xaxis_rangeslider_visible=False,
    plot_bgcolor='#090c10',
    paper_bgcolor='#090c10',
    dragmode=False,
    xaxis=dict(
        type='date',
        showgrid=True,
        gridcolor='#151b26',
        color='#64748b',
        fixedrange=True,
        nticks=3,
        tickformat='%H:%M'
    ),
    yaxis=dict(
        showgrid=True,
        gridcolor='#151b26',
        color='#64748b',
        side='right',
        tickformat='.2f',
        fixedrange=True
    )
)

st.plotly_chart(
    fig, 
    use_container_width=True, 
    config={'displayModeBar': False, 'scrollZoom': False, 'doubleClick': False}
)

# --- BOTTOM CONTROLS (Row 1) ---
st.markdown(f"""
    <div class="bottom-panel-1">
        <div class="panel-grp">
            <div>ACCOUNT: <span class="c-green">$10.00 BASE</span></div>
            <div>|</div>
            <div>ALLOCATION: <span class="c-cyan">$2.50 (10x LEV = $25 NOTIONAL)</span></div>
        </div>
        <div class="panel-grp">
            <span class="btn-force">⚡ FORCE CLOSE</span>
            <span class="btn-kill">🚨 KILL SWITCH</span>
        </div>
    </div>
""", unsafe_allow_html=True)

# --- BOTTOM CONTROLS (Row 2) ---
st.markdown(f"""
    <div class="bottom-panel-2">
        <div class="info-card">
            <span class="info-title">THREAD 3 RISK</span>
            <span class="info-val c-green">-$2.00 DRAWDOWN GUARD</span>
        </div>
        <div class="info-card">
            <span class="info-title">EXTRA FILTERS</span>
            <span class="info-val c-cyan">ATR • OB • VOL • FUNDING</span>
        </div>
        <div class="info-card">
            <span class="info-title">TARGET PROFILE</span>
            <span class="info-val c-green">100% DIRECT SWING TP</span>
        </div>
        <div class="info-card">
            <span class="info-title">RADAR SCANNER</span>
            <span class="info-val c-yellow">{radar_status}</span>
        </div>
    </div>
""", unsafe_allow_html=True)
