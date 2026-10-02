import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
import sqlite3
from datetime import datetime

# Full Mobile & Web App Viewport Setup
st.set_page_config(
    page_title="BTCUSDT RADAR",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom Exact Mobile + Desktop Theme CSS
st.markdown("""
    <style>
        /* Hide Streamlit default chrome */
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { 
            padding: 8px 10px !important; 
            max-width: 100% !important;
            background-color: #0c0f14; 
        }
        .stApp { background-color: #0c0f14; color: #d1d4dc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }

        /* Top HUD Bar */
        .radar-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 10px;
            background: #11151f;
            border: 1px solid #1a2230;
            border-radius: 6px;
            font-size: 11px;
            margin-bottom: 6px;
        }
        .radar-tag {
            color: #ffb703;
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .hud-item { display: flex; flex-direction: column; }
        .hud-lbl { font-size: 8px; color: #64748b; font-weight: 700; text-transform: uppercase; }
        .hud-val { font-weight: 700; font-size: 11px; }

        /* Bottom Floating Bar */
        .footer-hud {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 6px 10px;
            background: #0f131a;
            border: 1px solid #1a2230;
            border-radius: 6px;
            font-size: 9px;
            margin-top: 6px;
        }
        .c-green { color: #00e676 !important; font-weight: bold; }
        .c-red { color: #ff3b30 !important; font-weight: bold; }
        .c-cyan { color: #00e5ff !important; font-weight: bold; }
        .c-blue { color: #38bdf8 !important; font-weight: bold; }
    </style>
""", unsafe_allow_html=True)

# --- SQLite Vault Database ---
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

# Seed default history if empty
cur.execute("SELECT COUNT(*) FROM vault")
if cur.fetchone()[0] == 0:
    seed = [
        ("19:01", "LONG", 84758.0, "BE EXIT (50%) 🛡️", "(+$0.06)", 1),
        ("18:58", "LONG", 84758.0, "TP1 BOOK (50%) 🎯", "(+$0.30)", 1),
        ("18:23", "LONG", 84604.4, "TP2 FULL HIT 🔥", "(+$0.63)", 1),
        ("18:21", "LONG", 84604.4, "TP1 BOOK (50%) 🎯", "(+$0.31)", 1),
        ("12:55", "LONG", 85002.8, "SL HIT 🛑", "(-$0.85)", 0),
        ("09:51", "LONG", 84804.2, "TP2 FULL HIT 🔥", "(+$0.86)", 1),
        ("09:45", "LONG", 84804.2, "TP1 BOOK (50%) 🎯", "(+$0.43)", 1),
        ("08:20", "LONG", 84791.0, "BE EXIT (50%) 🛡️", "(+$0.06)", 1),
        ("08:16", "LONG", 84791.0, "TP1 BOOK (50%) 🎯", "(+$0.33)", 1),
        ("07:41", "LONG", 84534.6, "TP2 FULL HIT 🔥", "(+$0.53)", 1)
    ]
    cur.executemany("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)", seed)
    conn.commit()

# --- 100% Reliable Multi-Source Live Candles Feed ---
def fetch_live_candles():
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # 1. Binance US Public REST endpoint (Never blocked on Streamlit Cloud)
    try:
        url = "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=70"
        r = requests.get(url, headers=headers, timeout=3.5)
        raw = r.json()
        if isinstance(raw, list) and len(raw) > 10:
            df = pd.DataFrame(raw, columns=[
                't', 'o', 'h', 'l', 'c', 'v', 'ct', 'qa', 'tr', 'tb', 'tq', 'i'
            ])
            df['time'] = pd.to_datetime(df['t'], unit='ms')
            for col in ['o', 'h', 'l', 'c']:
                df[col] = df[col].astype(float)
            df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close'}, inplace=True)
            return df
    except Exception:
        pass

    # 2. Kraken Public Backup API
    try:
        url = "https://api.kraken.com/0/public/OHLC?pair=XBTUSDT&interval=5"
        r = requests.get(url, headers=headers, timeout=3.5)
        data = r.json().get('result', {}).get('XBTUSDT', [])
        if data and len(data) > 10:
            df = pd.DataFrame(data[-70:], columns=['t', 'o', 'h', 'l', 'c', 'vw', 'vol', 'cnt'])
            df['time'] = pd.to_datetime(df['t'].astype(float), unit='s')
            for col in ['o', 'h', 'l', 'c']:
                df[col] = df[col].astype(float)
            df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close'}, inplace=True)
            return df
    except Exception:
        pass

    # 3. Coinbase Pro Backup API
    try:
        url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=300"
        r = requests.get(url, headers=headers, timeout=3.5)
        data = r.json()
        if isinstance(data, list) and len(data) > 10:
            df = pd.DataFrame(data[:70], columns=['t', 'l', 'h', 'o', 'c', 'v'])
            df['time'] = pd.to_datetime(df['t'], unit='s')
            for col in ['o', 'h', 'l', 'c']:
                df[col] = df[col].astype(float)
            df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close'}, inplace=True)
            df = df.iloc[::-1].reset_index(drop=True)
            return df
    except Exception:
        pass

    return pd.DataFrame()

df = fetch_live_candles()

# Agar kisi wajah se teeno API ruk jayein, safely handle:
if df.empty:
    st.warning("Connecting live exchange data... Click Refresh in 5 seconds.")
    st.stop()

# --- Radar Signal Logic (EMA + Breakout Check) ---
df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

last = df.iloc[-1]
prev = df.iloc[-2]
cur_p = round(float(last['close']), 2)
high_p = float(last['high'])
low_p = float(last['low'])
atr = round(abs(high_p - low_p), 2) or 120.0

# Condition Check: Jab signal trigger hoga tabhi ENTRY, SL, TP dikhayega
has_signal = False
signal_type = "MONITORING PULLBACKS"
entry_val, sl_val, tp_val = "--", "--", "--"

if last['ema9'] > last['ema21'] and last['close'] > prev['high']:
    has_signal = True
    signal_type = "PRE-SIGNAL LONG"
    entry_val = f"${cur_p}"
    sl_val = f"${round(cur_p - (atr * 1.5), 1)}"
    tp_val = f"${round(cur_p + (atr * 2.5), 1)}"
elif last['ema9'] < last['ema21'] and last['close'] < prev['low']:
    has_signal = True
    signal_type = "PRE-SIGNAL SHORT"
    entry_val = f"${cur_p}"
    sl_val = f"${round(cur_p + (atr * 1.5), 1)}"
    tp_val = f"${round(cur_p - (atr * 2.5), 1)}"

# --- TOP RADAR HUD (Exact Mobile Screenshot 1000115045) ---
st.markdown(f"""
    <div class="radar-header">
        <div class="radar-tag">⚡ BTCUSDT RADAR</div>
        <div class="hud-item">
            <span class="hud-lbl">RADAR STATUS</span>
            <span class="hud-val c-blue">{signal_type}</span>
        </div>
        <div class="hud-item">
            <span class="hud-lbl">ENTRY LEVEL</span>
            <span class="hud-val c-cyan">{entry_val}</span>
        </div>
        <div class="hud-item">
            <span class="hud-lbl">STRUCTURE SL</span>
            <span class="hud-val c-red">{sl_val}</span>
        </div>
        <div class="hud-item">
            <span class="hud-lbl">DIRECT TP</span>
            <span class="hud-val c-green">{tp_val}</span>
        </div>
    </div>
""", unsafe_allow_html=True)

# --- EXACT CANDLESTICK CHART (Mobile Vertical Fit) ---
fig = go.Figure()

# Real Candlesticks with Thin Wicks (Green / Red)
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

# Signal Lines Tabhi banegi jab Signal ACTIVE hoga
if has_signal:
    fig.add_hline(y=float(tp_val.replace('$', '')), line_dash="dash", line_color="#00e676", line_width=1.3,
                  annotation_text=f"PRE-SIGNAL DIRECT TP: {tp_val}", annotation_position="top right",
                  annotation_font_color="#00e676", annotation_bgcolor="#0c0f14")

    fig.add_hline(y=float(entry_val.replace('$', '')), line_dash="dash", line_color="#00e5ff", line_width=1.3,
                  annotation_text=f"PRE-SIGNAL ENTRY: {entry_val}", annotation_position="right",
                  annotation_font_color="#00e5ff", annotation_bgcolor="#0c0f14")

    fig.add_hline(y=float(sl_val.replace('$', '')), line_dash="dash", line_color="#ff3b30", line_width=1.3,
                  annotation_text=f"PRE-SIGNAL SL: {sl_val}", annotation_position="bottom right",
                  annotation_font_color="#ff3b30", annotation_bgcolor="#0c0f14")

# Current Live Price Tag (Right Edge)
fig.add_hline(y=cur_p, line_dash="dot", line_color="#ff3b30", line_width=1,
              annotation_text=f" {cur_p} ", annotation_position="right",
              annotation_font_color="#ffffff", annotation_bgcolor="#ff3b30")

fig.update_layout(
    height=540,
    margin=dict(l=5, r=70, t=10, b=10),
    xaxis_rangeslider_visible=False,
    plot_bgcolor='#090c10',
    paper_bgcolor='#090c10',
    xaxis=dict(
        showgrid=True, gridcolor='#151b26', color='#64748b',
        rangeslider=dict(visible=False), type='date'
    ),
    yaxis=dict(
        showgrid=True, gridcolor='#151b26', color='#64748b', 
        side='right', tickformat='.2f'
    )
)

st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})

# --- BOTTOM BAR STATS (Exact Mobile Screenshot 1000115045) ---
st.markdown("""
    <div class="footer-hud">
        <div>CAPITAL: <span class="c-green">$10.00</span> | MARGIN: <span class="c-cyan">$2.50 (10x)</span> | WIN RATE: <span class="c-green">80%</span></div>
        <div>
            <span style="background:#501317; color:#ff8b94; padding:3px 6px; border-radius:4px; font-weight:bold;">⚠️ FORCE CLOSE</span>
        </div>
    </div>
""", unsafe_allow_html=True)

# --- SQLITE VAULT (PROTECTED TRADES) ---
with st.expander("🔒 SQLITE VAULT (PROTECTED TRADES)", expanded=False):
    cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
    rows = cur.fetchall()
    total_trades = len(rows)
    wins = sum(1 for r in rows if r[5] == 1)
    win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

    col_v1, col_v2 = st.columns(2)
    col_v1.write(f"**Total Trades:** {total_trades}")
    col_v2.markdown(f"**Win Rate:** <span class='c-green'>{win_rate}%</span>", unsafe_allow_html=True)

    for r in rows:
        badge_color = "#00e676" if r[5] == 1 else "#ff5252"
        st.markdown(
            f"<div style='display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #1a2230; font-size:11px;'>"
            f"<span>{r[0]} <b style='color:#00e5ff'>{r[1]}</b> @ ${r[2]}</span>"
            f"<span style='color:{badge_color}; font-weight:bold;'>{r[3]} {r[4]}</span>"
            f"</div>",
            unsafe_allow_html=True
        )

    if st.button("🗑️ ONE-CLICK CLEAR VAULT"):
        cur.execute("DELETE FROM vault")
        conn.commit()
        st.rerun()
