import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sqlite3
from datetime import datetime

st.set_page_config(
    page_title="BTCUSDT RADAR 48H PRO",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Har 3 second me live market pulse
st_autorefresh(interval=3000, limit=None, key="pro_market_pulse")

# Styling: Matte Dark UI + Non-flickering CSS
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { 
            padding: 2px 4px !important; 
            max-width: 100% !important;
            background-color: #080a0f; 
        }
        .stApp { 
            background-color: #080a0f; 
            color: #d1d4dc; 
            font-family: -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, sans-serif;
            -webkit-user-select: none;
        }

        /* Top HUD Bar */
        .top-radar-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 10px;
            background: #0d1118;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            font-size: 10px;
        }
        .bar-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .badge-online { background: #089981; color: #ffffff; font-weight: 800; font-size: 8px; padding: 2px 5px; border-radius: 3px; letter-spacing: 0.5px; }
        .badge-tf { background: #131924; color: #38bdf8; font-weight: 800; font-size: 8px; padding: 2px 6px; border-radius: 3px; border: 1px solid #1e293b; }
        .hud-cell { display: flex; flex-direction: column; }
        .cell-lbl { font-size: 7px; color: #565f70; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
        .cell-val { font-weight: 800; font-size: 11px; font-variant-numeric: tabular-nums; }

        /* Pure CSS Client-Side Modal */
        #vault-modal-checkbox { display: none; }
        .vault-modal-btn {
            background: #141b27;
            border: 1px solid #232f42;
            color: #38bdf8;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 700;
            cursor: pointer;
            display: inline-block;
            transition: all 0.2s ease;
        }
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(4, 6, 10, 0.88);
            backdrop-filter: blur(6px);
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
            border-radius: 10px;
            width: 100%;
            max-width: 380px;
            padding: 16px;
            box-shadow: 0 15px 35px rgba(0,0,0,0.95);
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 10px;
            margin-bottom: 12px;
            font-weight: 800;
            font-size: 12px;
        }
        .btn-modal-close {
            color: #64748b;
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
            padding: 5px 10px;
            border-top: 1px solid rgba(255, 255, 255, 0.06);
            font-size: 9px;
            margin-top: 2px;
        }
        .panel-grp { display: flex; align-items: center; gap: 8px; }
        .btn-force {
            border: 1px solid rgba(202, 138, 4, 0.4);
            color: #fbbf24;
            background: rgba(202, 138, 4, 0.12);
            padding: 3px 6px;
            border-radius: 3px;
            font-weight: 700;
            font-size: 8px;
        }
        .btn-kill {
            border: 1px solid rgba(220, 38, 38, 0.4);
            color: #f87171;
            background: rgba(220, 38, 38, 0.12);
            padding: 3px 6px;
            border-radius: 3px;
            font-weight: 700;
            font-size: 8px;
        }

        .bottom-panel-2 {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 4px;
            background: #06080c;
            padding: 4px 6px 6px 6px;
            font-size: 8px;
        }
        .info-card {
            background: #0d121a;
            border: 1px solid #161e2a;
            padding: 5px 7px;
            border-radius: 4px;
            display: flex;
            flex-direction: column;
        }
        .info-title { color: #475569; font-size: 7px; font-weight: 700; }
        .info-val { font-weight: 800; font-size: 8.5px; margin-top: 1px; }

        .c-green { color: #089981 !important; font-weight: bold; }
        .c-red { color: #f23645 !important; font-weight: bold; }
        .c-cyan { color: #00e5ff !important; font-weight: bold; }
        .c-yellow { color: #f59e0b !important; font-weight: bold; }
    </style>
""", unsafe_allow_html=True)

# Database Setup (With Active Trade Persistence Table)
conn = sqlite3.connect('trades_vault.db', check_same_thread=False)
cur = conn.cursor()

# Closed Trades Vault
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

# PERSISTENT ACTIVE TRADE TABLE (Server reload par bhi signal gayab nahi hoga)
cur.execute('''
    CREATE TABLE IF NOT EXISTS active_signal (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        direction TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        basis TEXT,
        start_time TEXT
    )
''')
conn.commit()

# Current Vault Count
cur.execute("SELECT COUNT(*) FROM vault")
vault_count = cur.fetchone()[0]

# 48H Market Data
def get_48h_market_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    urls = [
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=600",
        "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=600"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers=headers, timeout=3.0).json()
            if isinstance(r, list) and len(r) > 100:
                df = pd.DataFrame(r, columns=['t','o','h','l','c','v','ct','qa','tr','tb','tq','i'])
                df['time'] = pd.to_datetime(df['t'], unit='ms')
                for c in ['o','h','l','c','v']: df[c] = df[c].astype(float)
                df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                return df
        except Exception:
            continue
    return pd.DataFrame()

df = get_48h_market_data()
if df.empty:
    st.stop()

# Structure & Levels
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
if atr < 35: atr = 90.0

# Check if there is an EXISTING LOCKED SIGNAL in Database
cur.execute("SELECT direction, entry, sl, tp, basis, start_time FROM active_signal WHERE id = 1")
locked_trade = cur.fetchone()

has_signal = False
radar_status = "SCANNING"
entry_txt, sl_txt, tp_txt = "--", "--", "--"
entry_val, sl_val, tp_val = 0.0, 0.0, 0.0
trigger_basis_text = "SCANNING: Awaiting 48H Sweep / Structure Shift"

if locked_trade:
    # TRADE IS CURRENTLY ACTIVE & LOCKED
    direction, entry_val, sl_val, tp_val, trigger_basis_text, start_time = locked_trade
    has_signal = True
    radar_status = f"ACTIVE {direction}"
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"

    # LIVE SL / TP HIT DETECTION
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
    # NO ACTIVE TRADE: SCAN FOR NEW INSTITUTIONAL TRIGGER
    dist_to_48h_h = abs(cur_p - h48)
    dist_to_48h_l = abs(cur_p - l48)
    is_near_major_h = dist_to_48h_h < (atr * 2.0)
    is_near_major_l = dist_to_48h_l < (atr * 2.0)

    recent_swing_high = df['high'].iloc[-12:-2].max()
    recent_swing_low = df['low'].iloc[-12:-2].min()
    vol_avg = df['volume'].iloc[-10:-1].mean()
    is_displacement = last['volume'] > (vol_avg * 1.3)

    new_signal = False
    new_dir = ""
    new_basis = ""

    # Bullish Confluence
    if not is_near_major_h:
        if prev['low'] <= pdl and last['close'] > pdl:
            new_signal = True
            new_dir = "LONG"
            new_basis = "🎯 BASIS: PDL LIQUIDITY SWEEP & RECLAIM (SELL-STOP ABSORPTION)"
            entry_val = cur_p
            sl_val = round(cur_p - (atr * 1.6), 1)
            tp_val = round(min(cur_p + (atr * 2.8), h48), 1)
        elif last['close'] > recent_swing_high and is_displacement and last['ema9'] > last['ema21']:
            new_signal = True
            new_dir = "LONG"
            new_basis = "⚡ BASIS: 5M BULLISH MSS BREAKOUT + SMART MONEY VOLUME SURGE"
            entry_val = cur_p
            sl_val = round(cur_p - (atr * 1.6), 1)
            tp_val = round(min(cur_p + (atr * 2.8), h48), 1)

    # Bearish Confluence
    if not is_near_major_l and not new_signal:
        if prev['high'] >= pdh and last['close'] < pdh:
            new_signal = True
            new_dir = "SHORT"
            new_basis = "🛑 BASIS: PDH BUY-SIDE LIQUIDITY SWEEP (BULL TRAP REJECTION)"
            entry_val = cur_p
            sl_val = round(cur_p + (atr * 1.6), 1)
            tp_val = round(max(cur_p - (atr * 2.8), l48), 1)
        elif last['close'] < recent_swing_low and is_displacement and last['ema9'] < last['ema21']:
            new_signal = True
            new_dir = "SHORT"
            new_basis = "⚡ BASIS: 5M BEARISH MSS BREAKDOWN + INSTITUTIONAL SELLING"
            entry_val = cur_p
            sl_val = round(cur_p + (atr * 1.6), 1)
            tp_val = round(max(cur_p - (atr * 2.8), l48), 1)

    # If trigger verified, LOCK INTO DATABASE
    if new_signal:
        cur.execute("INSERT OR REPLACE INTO active_signal (id, direction, entry, sl, tp, basis, start_time) VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (new_dir, entry_val, sl_val, tp_val, new_basis, datetime.now().strftime('%H:%M')))
        conn.commit()
        st.rerun()

market_regime = "TRENDING" if abs(last['ema9'] - last['ema50']) > (atr * 1.5) else "PULLBACK REGIME"

# Fetch Vault
cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
rows = cur.fetchall()
total_trades = len(rows)
wins = sum(1 for r in rows if r[5] == 1)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

trade_items_html = ""
if total_trades == 0:
    trade_items_html = "<div style='font-size:10px; color:#64748b; text-align:center; padding:16px 0;'>No executed trades yet. Monitoring institutional triggers...</div>"
else:
    for r in rows:
        clr = "#089981" if r[5] == 1 else "#f23645"
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
            <span class="badge-online">LIVE</span>
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

# Chart Subplots
fig = make_subplots(
    rows=2, cols=1,
    shared_xaxes=True,
    vertical_spacing=0.02,
    row_heights=[0.84, 0.16]
)

# Matte Candles
fig.add_trace(go.Candlestick(
    x=df['time'],
    open=df['open'],
    high=df['high'],
    low=df['low'],
    close=df['close'],
    increasing_line_color='#089981',
    decreasing_line_color='#f23645',
    increasing_fillcolor='#089981',
    decreasing_fillcolor='#f23645',
    line_width=1.1,
    name="BTC"
), row=1, col=1)

# Volume
colors = ['rgba(8, 153, 129, 0.28)' if c >= o else 'rgba(242, 54, 69, 0.28)' for o, c in zip(df['open'], df['close'])]
fig.add_trace(go.Bar(
    x=df['time'],
    y=df['volume'],
    marker_color=colors,
    marker_line_width=0,
    name="Vol"
), row=2, col=1)

# LOCKED TARGET LINES (BINA SL/TP HIT HUE KABHI GAYAB NAHI HONGi)
if has_signal:
    fig.add_hline(y=tp_val, line_dash="dash", line_color="#089981", line_width=1.2,
                  annotation_text=f"DIRECT TP: {tp_val}", annotation_position="top right",
                  annotation_font_color="#089981", annotation_bgcolor="#080a0f", row=1, col=1)

    fig.add_hline(y=entry_val, line_dash="dash", line_color="#00e5ff", line_width=1.2,
                  annotation_text=f"ENTRY: {entry_val}", annotation_position="right",
                  annotation_font_color="#00e5ff", annotation_bgcolor="#080a0f", row=1, col=1)

    fig.add_hline(y=sl_val, line_dash="dash", line_color="#f23645", line_width=1.2,
                  annotation_text=f"SL: {sl_val}", annotation_position="bottom right",
                  annotation_font_color="#f23645", annotation_bgcolor="#080a0f", row=1, col=1)

# Live Price Tracker
fig.add_hline(y=cur_p, line_dash="dot", line_color="#f23645", line_width=1,
              annotation_text=f" {cur_p:.2f} ", annotation_position="right",
              annotation_font_color="#ffffff", annotation_bgcolor="#f23645", row=1, col=1)

# Basis Box on Top-Left
box_bg = "rgba(8, 153, 129, 0.2)" if "LONG" in radar_status else ("rgba(242, 54, 69, 0.2)" if "SHORT" in radar_status else "rgba(19, 25, 36, 0.6)")
box_border = "#089981" if "LONG" in radar_status else ("#f23645" if "SHORT" in radar_status else "#232f42")
text_clr = "#38bdf8" if has_signal else "#64748b"

fig.add_annotation(
    xref="paper", yref="paper",
    x=0.015, y=0.97,
    text=f"<b>{trigger_basis_text}</b>",
    showarrow=False,
    font=dict(size=9.5, color=text_clr, family="-apple-system, sans-serif"),
    bgcolor=box_bg,
    bordercolor=box_border,
    borderwidth=1,
    borderpad=4,
    row=1, col=1
)

latest_time = df['time'].iloc[-1]
start_viewport_time = df['time'].iloc[-75]

fig.update_layout(
    height=495,
    margin=dict(l=0, r=68, t=4, b=4),
    xaxis_rangeslider_visible=False,
    plot_bgcolor='#080a0f',
    paper_bgcolor='#080a0f',
    dragmode="pan",
    showlegend=False,
    xaxis=dict(
        type='date',
        range=[start_viewport_time, latest_time],
        showgrid=True,
        gridcolor='rgba(255, 255, 255, 0.035)',
        color='#565f70',
        fixedrange=False,
        nticks=4,
        tickformat='%H:%M'
    ),
    xaxis2=dict(
        showgrid=False,
        color='#565f70',
        fixedrange=False
    ),
    yaxis=dict(
        showgrid=True,
        gridcolor='rgba(255, 255, 255, 0.035)',
        color='#565f70',
        side='right',
        tickformat='.2f',
        fixedrange=False
    ),
    yaxis2=dict(
        showgrid=False,
        showticklabels=False,
        fixedrange=True
    )
)

st.plotly_chart(
    fig, 
    use_container_width=True, 
    config={
        'displayModeBar': False,
        'scrollZoom': True
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
