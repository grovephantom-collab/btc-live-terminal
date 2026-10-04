import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
import sqlite3
from datetime import datetime

st.set_page_config(
    page_title="BTCUSDT RADAR 48H",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Smooth refresh interval
st_autorefresh(interval=3000, limit=None, key="custom_radar_pulse")

# Styling: Pure Native Dark UI + Zero DOM Flickering
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { 
            padding: 3px 6px !important; 
            max-width: 100% !important;
            background-color: #080a0f !important; 
        }
        .stApp { 
            background-color: #080a0f !important; 
            color: #d1d4dc; 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            -webkit-user-select: none;
            -webkit-backface-visibility: hidden;
        }

        /* Top HUD Bar */
        .top-radar-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 5px 8px;
            background: #0d1118;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            font-size: 10px;
        }
        .bar-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .badge-online { background: #00e676; color: #000; font-weight: 900; font-size: 8px; padding: 2px 4px; border-radius: 2px; }
        .badge-tf { background: #131924; color: #38bdf8; font-weight: 800; font-size: 8px; padding: 2px 5px; border-radius: 2px; border: 1px solid #1e293b; }
        .hud-cell { display: flex; flex-direction: column; }
        .cell-lbl { font-size: 7px; color: #64748b; font-weight: 700; text-transform: uppercase; }
        .cell-val { font-weight: 800; font-size: 10px; }

        /* Pure CSS Modal (No Reload) */
        #vault-modal-checkbox { display: none; }
        .vault-modal-btn {
            background: #141b27;
            border: 1px solid #232f42;
            color: #38bdf8;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 700;
            cursor: pointer;
            display: inline-block;
        }
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(0, 0, 0, 0.88);
            backdrop-filter: blur(5px);
            z-index: 9999999;
            align-items: center;
            justify-content: center;
            padding: 16px;
            box-sizing: border-box;
        }
        #vault-modal-checkbox:checked ~ .modal-overlay {
            display: flex;
        }
        .modal-card {
            background: #0f141e;
            border: 1px solid #1c2636;
            border-radius: 8px;
            width: 100%;
            max-width: 380px;
            padding: 14px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.9);
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 8px;
            margin-bottom: 10px;
            font-weight: 800;
            font-size: 12px;
        }
        .btn-modal-close {
            color: #94a3b8;
            cursor: pointer;
            font-size: 16px;
            font-weight: 900;
        }

        /* Bottom Controls */
        .bottom-panel-1 {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #0b0f16;
            padding: 4px 8px;
            border-top: 1px solid rgba(255, 255, 255, 0.06);
            font-size: 9px;
            margin-top: 2px;
        }
        .panel-grp { display: flex; align-items: center; gap: 8px; }
        .btn-force {
            border: 1px solid rgba(202, 138, 4, 0.4);
            color: #fbbf24;
            background: rgba(202, 138, 4, 0.12);
            padding: 2px 5px;
            border-radius: 3px;
            font-weight: 700;
            font-size: 8px;
        }
        .btn-kill {
            border: 1px solid rgba(220, 38, 38, 0.4);
            color: #f87171;
            background: rgba(220, 38, 38, 0.12);
            padding: 2px 5px;
            border-radius: 3px;
            font-weight: 700;
            font-size: 8px;
        }

        .bottom-panel-2 {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 4px;
            background: #06080c;
            padding: 3px 6px 6px 6px;
            font-size: 8px;
        }
        .info-card {
            background: #0d121a;
            border: 1px solid #161e2a;
            padding: 3px 5px;
            border-radius: 3px;
            display: flex;
            flex-direction: column;
        }
        .info-title { color: #475569; font-size: 7px; font-weight: 700; }
        .info-val { font-weight: 800; font-size: 8.5px; margin-top: 1px; }

        .c-green { color: #00e676 !important; font-weight: bold; }
        .c-red { color: #ff3b30 !important; font-weight: bold; }
        .c-cyan { color: #00e5ff !important; font-weight: bold; }
        .c-yellow { color: #f59e0b !important; font-weight: bold; }
    </style>
""", unsafe_allow_html=True)

# Database Setup
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
cur.execute('''
    CREATE TABLE IF NOT EXISTS active_signal (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        direction TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        basis TEXT
    )
''')
conn.commit()

cur.execute("SELECT COUNT(*) FROM vault")
vault_count = cur.fetchone()[0]

# Multi-Source 48H Market Data Fetcher
def fetch_custom_candles():
    urls = [
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=576",
        "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=576"
    ]
    headers = {'User-Agent': 'Mozilla/5.0'}
    for u in urls:
        try:
            r = requests.get(u, headers=headers, timeout=2.5).json()
            if isinstance(r, list) and len(r) > 100:
                df = pd.DataFrame(r, columns=['t','o','h','l','c','v','ct','qa','tr','tb','tq','i'])
                # Sahi real-time parsing (Bug Fix for 1980 Year)
                df['time'] = pd.to_datetime(df['t'], unit='ms')
                for c in ['o','h','l','c','v']: 
                    df[c] = df[c].astype(float)
                df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                return df
        except Exception:
            continue
    return pd.DataFrame()

df = fetch_custom_candles()
if df.empty:
    st.info("⚡ Synchronizing 48H Market Candles...")
    st.stop()

# 48H & Daily Key Levels
daily_bars = df.iloc[-288:]
prev_day_bars = df.iloc[-576:-288] if len(df) >= 576 else df.iloc[:288]

pdh = float(prev_day_bars['high'].max())
pdl = float(prev_day_bars['low'].min())
h48 = float(df['high'].max())
l48 = float(df['low'].min())

df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()

last = df.iloc[-1]
prev = df.iloc[-2]
cur_p = round(float(last['close']), 2)
atr = round(abs(float(last['high']) - float(last['low'])), 2)
if atr < 35: atr = 85.0

# Persistent Signal Check
cur.execute("SELECT direction, entry, sl, tp, basis FROM active_signal WHERE id = 1")
locked = cur.fetchone()

has_signal = False
radar_status = "SCANNING"
entry_txt, sl_txt, tp_txt = "--", "--", "--"
entry_val, sl_val, tp_val = 0.0, 0.0, 0.0
trigger_basis_text = "SCANNING: Awaiting 48H Sweep / Structure Shift"

if locked:
    has_signal = True
    direction, entry_val, sl_val, tp_val, trigger_basis_text = locked
    radar_status = f"ACTIVE {direction}"
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"

    # Target Hit Check
    if direction == "LONG":
        if cur_p >= tp_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "LONG", entry_val, "TP HIT 🔥", "(+$0.65)", 1))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
            st.rerun()
        elif cur_p <= sl_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "LONG", entry_val, "SL HIT 🛑", "(-$0.80)", 0))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
            st.rerun()
    elif direction == "SHORT":
        if cur_p <= tp_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "SHORT", entry_val, "TP HIT 🔥", "(+$0.65)", 1))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
            st.rerun()
        elif cur_p >= sl_val:
            cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now().strftime('%H:%M'), "SHORT", entry_val, "SL HIT 🛑", "(-$0.80)", 0))
            cur.execute("DELETE FROM active_signal WHERE id = 1")
            conn.commit()
            has_signal = False
            st.rerun()
else:
    # New Confluence Evaluation
    recent_swing_h = df['high'].iloc[-12:-2].max()
    recent_swing_l = df['low'].iloc[-12:-2].min()
    vol_avg = df['volume'].iloc[-10:-1].mean()
    is_disp = last['volume'] > (vol_avg * 1.25)

    if prev['low'] <= pdl and last['close'] > pdl:
        has_signal = True
        direction = "LONG"
        trigger_basis_text = "🎯 BASIS: PDL LIQUIDITY SWEEP & RECLAIM"
        entry_val = cur_p
        sl_val = round(cur_p - (atr * 1.6), 1)
        tp_val = round(min(cur_p + (atr * 2.8), h48), 1)
    elif last['close'] > recent_swing_h and is_disp and last['ema9'] > last['ema21']:
        has_signal = True
        direction = "LONG"
        trigger_basis_text = "⚡ BASIS: 5M BULLISH MSS BREAKOUT + DISPLACEMENT"
        entry_val = cur_p
        sl_val = round(cur_p - (atr * 1.6), 1)
        tp_val = round(min(cur_p + (atr * 2.8), h48), 1)
    elif prev['high'] >= pdh and last['close'] < pdh:
        has_signal = True
        direction = "SHORT"
        trigger_basis_text = "🛑 BASIS: PDH LIQUIDITY SWEEP & REJECTION"
        entry_val = cur_p
        sl_val = round(cur_p + (atr * 1.6), 1)
        tp_val = round(max(cur_p - (atr * 2.8), l48), 1)
    elif last['close'] < recent_swing_l and is_disp and last['ema9'] < last['ema21']:
        has_signal = True
        direction = "SHORT"
        trigger_basis_text = "⚡ BASIS: 5M BEARISH MSS BREAKDOWN"
        entry_val = cur_p
        sl_val = round(cur_p + (atr * 1.6), 1)
        tp_val = round(max(cur_p - (atr * 2.8), l48), 1)

    if has_signal:
        radar_status = f"ACTIVE {direction}"
        entry_txt = f"${entry_val}"
        sl_txt = f"${sl_val}"
        tp_txt = f"${tp_val}"
        cur.execute("INSERT OR REPLACE INTO active_signal VALUES (1, ?, ?, ?, ?, ?)", (direction, entry_val, sl_val, tp_val, trigger_basis_text))
        conn.commit()

market_regime = "TRENDING" if abs(last['ema9'] - last['ema50']) > (atr * 1.5) else "PULLBACK REGIME"

# Fetch Vault Rows
cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
rows = cur.fetchall()
total_trades = len(rows)
wins = sum(1 for r in rows if r[5] == 1)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

trade_items_html = ""
if total_trades == 0:
    trade_items_html = "<div style='font-size:10px; color:#64748b; text-align:center; padding:16px 0;'>No executed trades yet. Monitoring 48H triggers...</div>"
else:
    for r in rows:
        clr = "#00e676" if r[5] == 1 else "#ff3b30"
        trade_items_html += f"""
            <div style='display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.06); font-size:10px;'>
                <span>{r[0]} <b style='color:#00e5ff'>{r[1]}</b> @ ${r[2]}</span>
                <span style='color:{clr}; font-weight:bold;'>{r[3]} {r[4]}</span>
            </div>
        """

# TOP HUD
st.markdown(f"""
    <input type="checkbox" id="vault-modal-checkbox">
    <div class="top-radar-bar">
        <div class="bar-left">
            <span class="badge-online">ONLINE</span>
            <span class="badge-tf">5M • 48H</span>
            <div class="hud-cell">
                <span class="cell-lbl">RADAR STATUS</span>
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
            <label for="vault-modal-checkbox" class="vault-modal-btn">📜 VAULT ({vault_count})</label>
        </div>
    </div>

    <!-- ZERO-RELOAD MODAL -->
    <div class="modal-overlay">
        <div class="modal-card">
            <div class="modal-header">
                <span>🔒 SQLITE VAULT (PROTECTED TRADES)</span>
                <label for="vault-modal-checkbox" class="btn-modal-close">✕</label>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:10px; font-weight:bold; margin-bottom:8px;">
                <span>Total Protected: {total_trades}</span>
                <span>Win Rate: <span class="c-green">{win_rate}%</span></span>
            </div>
            <div style="max-height: 220px; overflow-y: auto;">
                {trade_items_html}
            </div>
        </div>
    </div>
""", unsafe_allow_html=True)

# =========================================================================
# OWN CUSTOM CANDLESTICK CHART (NO TRADINGVIEW LOGO / NO WHITE IFRAME)
# =========================================================================
fig = go.Figure()

# Custom Green & Red Candlesticks
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
    line_width=1.1,
    name="BTC"
))

# Signal Lines
if has_signal:
    fig.add_hline(y=tp_val, line_dash="dash", line_color="#00e676", line_width=1.3,
                  annotation_text=f"DIRECT TP: {tp_val}", annotation_position="top right",
                  annotation_font_color="#00e676", annotation_bgcolor="#080a0f")

    fig.add_hline(y=entry_val, line_dash="dash", line_color="#00e5ff", line_width=1.3,
                  annotation_text=f"ENTRY: {entry_val}", annotation_position="right",
                  annotation_font_color="#00e5ff", annotation_bgcolor="#080a0f")

    fig.add_hline(y=sl_val, line_dash="dash", line_color="#ff3b30", line_width=1.3,
                  annotation_text=f"SL: {sl_val}", annotation_position="bottom right",
                  annotation_font_color="#ff3b30", annotation_bgcolor="#080a0f")

# Current Live Price Tag
fig.add_hline(y=cur_p, line_dash="dot", line_color="#ff3b30", line_width=1,
              annotation_text=f" {cur_p:.2f} ", annotation_position="right",
              annotation_font_color="#ffffff", annotation_bgcolor="#dc2626")

# Basis Box on Top-Left
box_bg = "rgba(0, 230, 118, 0.15)" if "LONG" in radar_status else ("rgba(255, 59, 48, 0.15)" if "SHORT" in radar_status else "rgba(19, 25, 36, 0.6)")
box_border = "#00e676" if "LONG" in radar_status else ("#ff3b30" if "SHORT" in radar_status else "#232f42")

fig.add_annotation(
    xref="paper", yref="paper",
    x=0.015, y=0.97,
    text=f"<b>{trigger_basis_text}</b>",
    showarrow=False,
    font=dict(size=9.5, color="#38bdf8" if has_signal else "#64748b"),
    bgcolor=box_bg,
    bordercolor=box_border,
    borderwidth=1,
    borderpad=4
)

# Viewport range setup (Last 75 candles active view, scroll enables 48h)
latest_t = df['time'].iloc[-1]
start_t = df['time'].iloc[-75]

fig.update_layout(
    height=500,
    margin=dict(l=0, r=65, t=4, b=4),
    xaxis_rangeslider_visible=False,
    plot_bgcolor='#080a0f',
    paper_bgcolor='#080a0f',
    dragmode="pan",
    xaxis=dict(
        type='date',
        range=[start_t, latest_t],
        showgrid=True,
        gridcolor='rgba(255, 255, 255, 0.04)',
        color='#64748b',
        fixedrange=False,
        nticks=4,
        tickformat='%H:%M'
    ),
    yaxis=dict(
        showgrid=True,
        gridcolor='rgba(255, 255, 255, 0.04)',
        color='#64748b',
        side='right',
        tickformat='.2f',
        fixedrange=False
    )
)

st.plotly_chart(
    fig, 
    use_container_width=True, 
    config={
        'displayModeBar': False,
        'scrollZoom': True,
        'responsive': True
    }
)

# Bottom Panels
st.markdown(f"""
    <div class="bottom-panel-1">
        <div class="panel-grp">
            <div>ACCOUNT: <span class="c-green">$10.00 BASE</span></div>
            <div>|</div>
            <div>ALLOCATION: <span class="c-cyan">$2.50 (10x LEV)</span></div>
        </div>
        <div class="panel-grp">
            <span class="btn-force">⚡ FORCE CLOSE</span>
            <span class="btn-kill">🚨 KILL SWITCH</span>
        </div>
    </div>
""", unsafe_allow_html=True)

st.markdown(f"""
    <div class="bottom-panel-2">
        <div class="info-card">
            <span class="info-title">THREAD 3 RISK</span>
            <span class="info-val c-green">-$2.00 GUARD</span>
        </div>
        <div class="info-card">
            <span class="info-title">EXTRA FILTERS</span>
            <span class="info-val c-cyan">48H • BOS • APR</span>
        </div>
        <div class="info-card">
            <span class="info-title">REGIME DETECTOR</span>
            <span class="info-val c-green">{market_regime}</span>
        </div>
        <div class="info-card">
            <span class="info-title">RADAR SCANNER</span>
            <span class="info-val c-yellow">{radar_status}</span>
        </div>
    </div>
""", unsafe_allow_html=True)
