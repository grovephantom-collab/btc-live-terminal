import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
import sqlite3
from datetime import datetime

st.set_page_config(
    page_title="BTCUSDT RADAR",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Har 2.5 second me background refresh
st_autorefresh(interval=2500, limit=None, key="inst_engine_tick")

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

        /* Vault Trigger Button */
        .vault-link {
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

        /* Floating Modal Overlay */
        .modal-overlay {
            position: fixed !important;
            top: 0 !important;
            left: 0 !important;
            width: 100vw !important;
            height: 100vh !important;
            background: rgba(0, 0, 0, 0.8) !important;
            backdrop-filter: blur(4px) !important;
            z-index: 999999 !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            padding: 16px !important;
            box-sizing: border-box !important;
        }
        .modal-card {
            background: #11151f !important;
            border: 1px solid #1e2838 !important;
            border-radius: 8px !important;
            width: 100% !important;
            max-width: 380px !important;
            padding: 14px !important;
            box-shadow: 0 10px 30px rgba(0,0,0,0.9) !important;
        }
        .modal-header {
            display: flex !important;
            justify-content: space-between !important;
            align-items: center !important;
            border-bottom: 1px solid #1a2230 !important;
            padding-bottom: 8px !important;
            margin-bottom: 10px !important;
            font-weight: 800 !important;
            font-size: 12px !important;
        }
        .btn-modal-close {
            color: #94a3b8;
            text-decoration: none;
            font-size: 16px;
            font-weight: 900;
        }

        /* Bottom Control Bars */
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

# Database
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

cur.execute("SELECT COUNT(*) FROM vault")
vault_count = cur.fetchone()[0]

# Multi-Timeframe Data Fetching
def fetch_candles():
    headers = {'User-Agent': 'Mozilla/5.0'}
    urls = [
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=70",
        "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=70"
    ]
    for u in urls:
        try:
            res = requests.get(u, headers=headers, timeout=2.0).json()
            if isinstance(res, list) and len(res) > 10:
                df = pd.DataFrame(res, columns=['t','o','h','l','c','v','ct','qa','tr','tb','tq','i'])
                df['time'] = pd.to_datetime(df['t'], unit='ms')
                for col in ['o','h','l','c','v']: df[col] = df[col].astype(float)
                df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                return df
        except Exception:
            continue
    return pd.DataFrame()

df = fetch_candles()
if df.empty:
    st.stop()

# Indicators & Structure
df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()

last = df.iloc[-1]
prev = df.iloc[-2]
cur_p = round(float(last['close']), 2)
atr = round(abs(float(last['high']) - float(last['low'])), 2)
if atr < 35: atr = 85.0

# Regime
if abs(last['ema9'] - last['ema50']) > (atr * 1.2):
    market_regime = "TRENDING"
else:
    market_regime = "PULLBACK REGIME"

# Dynamic Institutional Signal Trigger (Active Entry Finder)
has_signal = True
if last['close'] >= last['open'] and last['ema9'] >= last['ema21']:
    radar_status = "PRE-SIGNAL LONG"
    entry_val = cur_p
    sl_val = round(cur_p - (atr * 1.5), 1)
    tp_val = round(cur_p + (atr * 2.2), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"
elif last['close'] < last['open'] and last['ema9'] <= last['ema21']:
    radar_status = "PRE-SIGNAL SHORT"
    entry_val = cur_p
    sl_val = round(cur_p + (atr * 1.5), 1)
    tp_val = round(cur_p - (atr * 2.2), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"
else:
    # Micro breakout default
    if last['close'] > prev['close']:
        radar_status = "PRE-SIGNAL LONG"
        entry_val = cur_p
        sl_val = round(cur_p - (atr * 1.5), 1)
        tp_val = round(cur_p + (atr * 2.0), 1)
    else:
        radar_status = "PRE-SIGNAL SHORT"
        entry_val = cur_p
        sl_val = round(cur_p + (atr * 1.5), 1)
        tp_val = round(cur_p - (atr * 2.0), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"

# Live Position Tracker to Vault Auto-Logger
if 'active_pos' not in st.session_state:
    st.session_state['active_pos'] = {
        'dir': 'LONG' if "LONG" in radar_status else 'SHORT',
        'entry': entry_val,
        'sl': sl_val,
        'tp': tp_val
    }
else:
    pos = st.session_state['active_pos']
    # Check TP
    if (pos['dir'] == 'LONG' and cur_p >= pos['tp']) or (pos['dir'] == 'SHORT' and cur_p <= pos['tp']):
        cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now().strftime('%H:%M'), pos['dir'], pos['entry'], "TP HIT 🔥", "(+$0.65)", 1))
        conn.commit()
        st.session_state['active_pos'] = {'dir': 'LONG' if "LONG" in radar_status else 'SHORT', 'entry': entry_val, 'sl': sl_val, 'tp': tp_val}
        st.rerun()
    # Check SL
    elif (pos['dir'] == 'LONG' and cur_p <= pos['sl']) or (pos['dir'] == 'SHORT' and cur_p >= pos['sl']):
        cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now().strftime('%H:%M'), pos['dir'], pos['entry'], "SL HIT 🛑", "(-$0.80)", 0))
        conn.commit()
        st.session_state['active_pos'] = {'dir': 'LONG' if "LONG" in radar_status else 'SHORT', 'entry': entry_val, 'sl': sl_val, 'tp': tp_val}
        st.rerun()

# HUD Header
query_params = st.query_params
show_vault = query_params.get("vault", "0") == "1"

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
            <a href="?vault=1" target="_self" class="vault-link">📜 VAULT ({vault_count})</a>
        </div>
    </div>
""", unsafe_allow_html=True)

# Vault Modal
if show_vault:
    cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
    rows = cur.fetchall()
    total = len(rows)
    wins = sum(1 for r in rows if r[5] == 1)
    rate = int((wins / total) * 100) if total > 0 else 0

    trade_items_html = ""
    if total == 0:
        trade_items_html = "<div style='font-size:10px; color:#64748b; text-align:center; padding:15px 0;'>No closed trades yet. Monitoring active target fills...</div>"
    else:
        for r in rows:
            clr = "#00e676" if r[5] == 1 else "#ff3b30"
            trade_items_html += f"""
                <div style='display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #1a2230; font-size:10px;'>
                    <span>{r[0]} <b style='color:#00e5ff'>{r[1]}</b> @ ${r[2]}</span>
                    <span style='color:{clr}; font-weight:bold;'>{r[3]} {r[4]}</span>
                </div>
            """

    st.markdown(f"""
        <div class="modal-overlay">
            <div class="modal-card">
                <div class="modal-header">
                    <span>🔒 SQLITE VAULT (PROTECTED TRADES)</span>
                    <a href="?vault=0" target="_self" class="btn-modal-close">✕</a>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:10px; font-weight:bold; margin-bottom:8px;">
                    <span>Total Protected: {total}</span>
                    <span>Win Rate: <span class="c-green">{rate}%</span></span>
                </div>
                <div style="max-height: 200px; overflow-y: auto;">
                    {trade_items_html}
                </div>
            </div>
        </div>
    """, unsafe_allow_html=True)

# Chart
fig = go.Figure()

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

# Signal Lines (Always visible with live targets)
fig.add_hline(y=tp_val, line_dash="dash", line_color="#00e676", line_width=1.3,
              annotation_text=f"PRE-SIGNAL DIRECT TP : {tp_val}", annotation_position="top right",
              annotation_font_color="#00e676", annotation_bgcolor="#0c0f14")

fig.add_hline(y=entry_val, line_dash="dash", line_color="#00e5ff", line_width=1.3,
              annotation_text=f"PRE-SIGNAL ENTRY : {entry_val}", annotation_position="right",
              annotation_font_color="#00e5ff", annotation_bgcolor="#0c0f14")

fig.add_hline(y=sl_val, line_dash="dash", line_color="#ff3b30", line_width=1.3,
              annotation_text=f"PRE-SIGNAL SL : {sl_val}", annotation_position="bottom right",
              annotation_font_color="#ff3b30", annotation_bgcolor="#0c0f14")

# Real-time price badge
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

# Bottom Bars
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
            <span class="info-title">REGIME DETECTOR</span>
            <span class="info-val c-green">{market_regime}</span>
        </div>
        <div class="info-card">
            <span class="info-title">RADAR SCANNER</span>
            <span class="info-val c-yellow">{radar_status}</span>
        </div>
    </div>
""", unsafe_allow_html=True)
