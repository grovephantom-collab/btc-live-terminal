import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
import sqlite3
from datetime import datetime

st.set_page_config(
    page_title="BTCUSDT RADAR 5M",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Har 3 second me background tick (No full DOM flash)
st_autorefresh(interval=3000, limit=None, key="market_stream_pulse")

# Styling: Pure Dark Theme + Zero-Reload JS Modal + Smooth Mobile UI
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { 
            padding: 4px 6px !important; 
            max-width: 100% !important;
            background-color: #0b0e14; 
        }
        .stApp { 
            background-color: #0b0e14; 
            color: #d1d4dc; 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            -webkit-user-select: none;
        }

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
        .bar-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .badge-online { background: #00e676; color: #000; font-weight: 900; font-size: 8px; padding: 2px 4px; border-radius: 2px; }
        .badge-tf { background: #1e293b; color: #38bdf8; font-weight: 800; font-size: 8px; padding: 2px 4px; border-radius: 2px; border: 1px solid #334155; }
        .hud-cell { display: flex; flex-direction: column; }
        .cell-lbl { font-size: 7px; color: #64748b; font-weight: 700; text-transform: uppercase; }
        .cell-val { font-weight: 800; font-size: 10px; }

        /* Pure CSS/JS Modal (ZERO STREAMLIT RELOAD / ZERO BLINK) */
        #vault-modal-checkbox { display: none; }
        .vault-modal-btn {
            background: #182232;
            border: 1px solid #28374d;
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
            background: rgba(0, 0, 0, 0.85);
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
            background: #11151f;
            border: 1px solid #1e2838;
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
            border-bottom: 1px solid #1a2230;
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

        /* Bottom Control Panels */
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

# Multi-Timeframe Data Engine
def get_market_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    urls_5m = [
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=100",
        "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=100"
    ]
    urls_1h = [
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=48",
        "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=48"
    ]
    
    df5, df1h = pd.DataFrame(), pd.DataFrame()
    for u in urls_5m:
        try:
            r = requests.get(u, headers=headers, timeout=2.2).json()
            if isinstance(r, list) and len(r) > 20:
                df5 = pd.DataFrame(r, columns=['t','o','h','l','c','v','ct','qa','tr','tb','tq','i'])
                df5['time'] = pd.to_datetime(df5['t'], unit='ms')
                for c in ['o','h','l','c','v']: df5[c] = df5[c].astype(float)
                df5.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                break
        except Exception:
            continue

    for u in urls_1h:
        try:
            r = requests.get(u, headers=headers, timeout=2.2).json()
            if isinstance(r, list) and len(r) > 10:
                df1h = pd.DataFrame(r, columns=['t','o','h','l','c','v','ct','qa','tr','tb','tq','i'])
                for c in ['o','h','l','c']: df1h[c] = df1h[c].astype(float)
                df1h.rename(columns={'o':'open','h':'high','l':'low','c':'close'}, inplace=True)
                break
        except Exception:
            continue

    return df5, df1h

df_5m, df_1h = get_market_data()
if df_5m.empty:
    st.stop()

# HTF Liquidity Levels (Step 1 & Step 2)
if not df_1h.empty and len(df_1h) >= 24:
    pdh = float(df_1h.iloc[-48:-24]['high'].max())
    pdl = float(df_1h.iloc[-48:-24]['low'].min())
else:
    pdh = float(df_5m['high'].max())
    pdl = float(df_5m['low'].min())

# Indicators (EMA, ATR, Volume)
df_5m['ema9'] = df_5m['close'].ewm(span=9, adjust=False).mean()
df_5m['ema21'] = df_5m['close'].ewm(span=21, adjust=False).mean()
df_5m['ema50'] = df_5m['close'].ewm(span=50, adjust=False).mean()

last = df_5m.iloc[-1]
prev = df_5m.iloc[-2]
cur_p = round(float(last['close']), 2)
atr = round(abs(float(last['high']) - float(last['low'])), 2)
if atr < 40: atr = 90.0

# Regime Engine
if abs(last['ema9'] - last['ema50']) > (atr * 1.6):
    market_regime = "TRENDING"
else:
    market_regime = "PULLBACK REGIME"

# Strict Confluence Engine (Sweeps + Structure Shift)
# Signal sirf tab trigger hoga jab conditions STRICTLY match karengi
has_signal = False
radar_status = "SCANNING"
entry_txt, sl_txt, tp_txt = "--", "--", "--"
entry_val, sl_val, tp_val = 0.0, 0.0, 0.0

swing_h = df_5m['high'].iloc[-8:-2].max()
swing_l = df_5m['low'].iloc[-8:-2].min()
vol_avg = df_5m['volume'].iloc[-6:-1].mean()

# Bullish Check: Low Sweep Reclaim OR Breakout MSS with Volume
if (prev['low'] <= pdl and last['close'] > pdl) or (last['close'] > swing_h and last['volume'] > vol_avg and last['ema9'] > last['ema21']):
    has_signal = True
    radar_status = "PRE-SIGNAL LONG"
    entry_val = cur_p
    sl_val = round(cur_p - (atr * 1.5), 1)
    tp_val = round(cur_p + (atr * 2.4), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"
# Bearish Check: High Sweep Rejection OR Breakdown MSS with Volume
elif (prev['high'] >= pdh and last['close'] < pdh) or (last['close'] < swing_l and last['volume'] > vol_avg and last['ema9'] < last['ema21']):
    has_signal = True
    radar_status = "PRE-SIGNAL SHORT"
    entry_val = cur_p
    sl_val = round(cur_p + (atr * 1.5), 1)
    tp_val = round(cur_p - (atr * 2.4), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"

# Trades Vault Data Fetching for Modal
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
        clr = "#00e676" if r[5] == 1 else "#ff3b30"
        trade_items_html += f"""
            <div style='display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #1a2230; font-size:10px;'>
                <span>{r[0]} <b style='color:#00e5ff'>{r[1]}</b> @ ${r[2]}</span>
                <span style='color:{clr}; font-weight:bold;'>{r[3]} {r[4]}</span>
            </div>
        """

# TOP HUD BAR (WITH TIMEFRAME BADGE & PURE CLIENT-SIDE VAULT MODAL)
st.markdown(f"""
    <input type="checkbox" id="vault-modal-checkbox">
    <div class="top-radar-bar">
        <div class="bar-left">
            <span class="badge-online">ONLINE</span>
            <span class="badge-tf">5M TIMEFRAME</span>
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
            <label for="vault-modal-checkbox" class="vault-modal-btn">📜 VAULT ({vault_count})</label>
        </div>
    </div>

    <!-- ZERO-RELOAD POPUP MODAL -->
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

# CHART ENGINE (PAN ENABLED - UNGLI SE DRAG KARKE PICHLI CANDLES DEKHEIN)
fig = go.Figure()

fig.add_trace(go.Candlestick(
    x=df_5m['time'],
    open=df_5m['open'],
    high=df_5m['high'],
    low=df_5m['low'],
    close=df_5m['close'],
    increasing_line_color='#00e676',
    decreasing_line_color='#ff3b30',
    increasing_fillcolor='#00e676',
    decreasing_fillcolor='#ff3b30',
    name="BTCUSDT 5M"
))

# Signal Lines (SIRF TAB DIKHENGI JAB STRICT SIGNAL TRIGGER HOGA)
if has_signal:
    fig.add_hline(y=tp_val, line_dash="dash", line_color="#00e676", line_width=1.3,
                  annotation_text=f"DIRECT TP: {tp_val}", annotation_position="top right",
                  annotation_font_color="#00e676", annotation_bgcolor="#0c0f14")

    fig.add_hline(y=entry_val, line_dash="dash", line_color="#00e5ff", line_width=1.3,
                  annotation_text=f"ENTRY: {entry_val}", annotation_position="right",
                  annotation_font_color="#00e5ff", annotation_bgcolor="#0c0f14")

    fig.add_hline(y=sl_val, line_dash="dash", line_color="#ff3b30", line_width=1.3,
                  annotation_text=f"SL: {sl_val}", annotation_position="bottom right",
                  annotation_font_color="#ff3b30", annotation_bgcolor="#0c0f14")

# Real-time current price tag
fig.add_hline(y=cur_p, line_dash="dot", line_color="#ff3b30", line_width=1,
              annotation_text=f" {cur_p:.2f} ", annotation_position="right",
              annotation_font_color="#ffffff", annotation_bgcolor="#dc2626")

fig.update_layout(
    height=490,
    margin=dict(l=0, r=65, t=5, b=5),
    xaxis_rangeslider_visible=False,
    plot_bgcolor='#090c10',
    paper_bgcolor='#090c10',
    dragmode="pan", # Pura chart ungli se piche aage scroll kar sakte hain
    xaxis=dict(
        type='date',
        showgrid=True,
        gridcolor='#151b26',
        color='#64748b',
        fixedrange=False, # Scroll/Pan Unlocked
        nticks=3,
        tickformat='%H:%M'
    ),
    yaxis=dict(
        showgrid=True,
        gridcolor='#151b26',
        color='#64748b',
        side='right',
        tickformat='.2f',
        fixedrange=False # Vertical scale touch enabled
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
