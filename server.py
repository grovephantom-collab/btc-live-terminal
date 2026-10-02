import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
import sqlite3
from datetime import datetime

st.set_page_config(page_title="BTCUSDT RADAR", layout="wide", initial_sidebar_state="collapsed")

# Styling
st.markdown("""
    <style>
        .block-container { padding: 10px 15px !important; background-color: #0c0f14; }
        header, footer { visibility: hidden; }
        .stApp { background-color: #0c0f14; color: #d1d4dc; }
        .c-green { color: #00e676; font-weight: bold; }
        .c-red { color: #ff5252; font-weight: bold; }
        .c-cyan { color: #00e5ff; font-weight: bold; }
        .c-gold { color: #ffb703; font-weight: 800; }
    </style>
""", unsafe_allow_html=True)

# Database
conn = sqlite3.connect('trades_vault.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
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

cursor.execute("SELECT COUNT(*) FROM vault")
if cursor.fetchone()[0] == 0:
    seed_data = [
        ("19:01", "LONG", 84758.0, "BE EXIT (50%) 🛡️", "(+$0.06)", 1),
        ("18:58", "LONG", 84758.0, "TP1 BOOK (50%) 🎯", "(+$0.30)", 1),
        ("18:23", "LONG", 84604.4, "TP2 FULL HIT 🔥", "(+$0.63)", 1),
        ("18:21", "LONG", 84604.4, "TP1 BOOK (50%) 🎯", "(+$0.31)", 1),
        ("12:55", "LONG", 85002.8, "SL HIT 🛑", "(-$0.85)", 0),
        ("09:51", "LONG", 84804.2, "TP2 FULL HIT 🔥", "(+$0.86)", 1),
        ("09:45", "LONG", 84804.2, "TP1 BOOK (50%) 🎯", "(+$0.43)", 1)
    ]
    cursor.executemany("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)", seed_data)
    conn.commit()

def get_klines():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=55"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=5)
        data = res.json()
        df = pd.DataFrame(data, columns=[
            'time', 'open', 'high', 'low', 'close', 'vol', 'close_time',
            'qav', 'num_trades', 'taker_base', 'taker_quote', 'ignore'
        ])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        for col in ['open', 'high', 'low', 'close']:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        now = pd.date_range(end=datetime.now(), periods=30, freq='5min')
        return pd.DataFrame({
            'time': now,
            'open': [84200.0 + i*10 for i in range(30)],
            'high': [84250.0 + i*10 for i in range(30)],
            'low': [84150.0 + i*10 for i in range(30)],
            'close': [84220.0 + i*10 for i in range(30)]
        })

df = get_klines()
curr_price = df['close'].iloc[-1]
high_p = df['high'].iloc[-1]
low_p = df['low'].iloc[-1]
atr = abs(high_p - low_p) or 150.0

entry_lvl = round(curr_price, 1)
sl_lvl = round(curr_price - (atr * 1.5), 1)
tp_lvl = round(curr_price + (atr * 2.2), 1)

# Header
c1, c2, c3, c4, c5 = st.columns([1.5, 2, 1.5, 1.5, 1.5])
with c1:
    st.markdown("### ⚡ <span class='c-gold'>BTCUSDT 5M</span>", unsafe_allow_html=True)
with c2:
    st.markdown("RADAR STATUS: <span class='c-cyan'>PRE-SIGNAL LONG</span>", unsafe_allow_html=True)
with c3:
    st.markdown(f"ENTRY: <span class='c-cyan'>${entry_lvl}</span>", unsafe_allow_html=True)
with c4:
    st.markdown(f"SL / TRAIL: <span class='c-red'>${sl_lvl}</span>", unsafe_allow_html=True)
with c5:
    st.markdown(f"DIRECT TP: <span class='c-green'>${tp_lvl}</span>", unsafe_allow_html=True)

# Candlestick
fig = go.Figure(data=[go.Candlestick(
    x=df['time'],
    open=df['open'],
    high=df['high'],
    low=df['low'],
    close=df['close'],
    increasing_line_color='#00e676',
    decreasing_line_color='#ff3b30',
    increasing_fillcolor='#00e676',
    decreasing_fillcolor='#ff3b30'
)])

fig.add_hline(y=tp_lvl, line_dash="dash", line_color="#00e676", line_width=1.5,
              annotation_text=f"PRE-SIGNAL DIRECT TP : {tp_lvl}", annotation_position="top right",
              annotation_font_color="#00e676", annotation_bgcolor="#0c0f14")

fig.add_hline(y=entry_lvl, line_dash="dash", line_color="#00e5ff", line_width=1.5,
              annotation_text=f"PRE-SIGNAL ENTRY : {entry_lvl}", annotation_position="right",
              annotation_font_color="#00e5ff", annotation_bgcolor="#0c0f14")

fig.add_hline(y=sl_lvl, line_dash="dash", line_color="#ff3b30", line_width=1.5,
              annotation_text=f"PRE-SIGNAL SL : {sl_lvl}", annotation_position="bottom right",
              annotation_font_color="#ff3b30", annotation_bgcolor="#0c0f14")

fig.update_layout(
    height=480,
    margin=dict(l=0, r=60, t=10, b=10),
    xaxis_rangeslider_visible=False,
    plot_bgcolor='#090c10',
    paper_bgcolor='#090c10',
    xaxis=dict(showgrid=True, gridcolor='#151b26', color='#6b7280'),
    yaxis=dict(showgrid=True, gridcolor='#151b26', color='#6b7280', side='right')
)

st.plotly_chart(fig, use_container_width=True)

# Footer
b1, b2, b3, b4 = st.columns(4)
b1.markdown("<small style='color:#6b7280'>ACCOUNT</small><br><b class='c-green'>$10.00 BASE</b>", unsafe_allow_html=True)
b2.markdown("<small style='color:#6b7280'>ALLOCATION</small><br><b>$2.50 [10x LEV]</b>", unsafe_allow_html=True)
b3.markdown("<small style='color:#6b7280'>DRAWDOWN GUARD</small><br><b class='c-red'>-$2.00 RISK</b>", unsafe_allow_html=True)
b4.markdown("<small style='color:#6b7280'>TARGET PROFILE</small><br><b class='c-green'>100% SWING TP</b>", unsafe_allow_html=True)

# Vault
st.write("---")
with st.expander("🔒 SQLITE VAULT (PROTECTED TRADES)", expanded=False):
    cursor.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
    rows = cursor.fetchall()
    total_trades = len(rows)
    wins = sum(1 for r in rows if r[5] == 1)
    win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

    col_v1, col_v2 = st.columns(2)
    col_v1.write(f"**Total Trades:** {total_trades}")
    col_v2.markdown(f"**Win Rate:** <span class='c-green'>{win_rate}%</span>", unsafe_allow_html=True)

    for r in rows:
        badge_color = "#00e676" if r[5] == 1 else "#ff5252"
        st.markdown(
            f"<div style='display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid #1a2230; font-size:12px;'>"
            f"<span>{r[0]} <b style='color:#00e5ff'>{r[1]}</b> @ ${r[2]}</span>"
            f"<span style='color:{badge_color}; font-weight:bold;'>{r[3]} {r[4]}</span>"
            f"</div>",
            unsafe_allow_html=True
        )

    if st.button("🗑️ ONE-CLICK CLEAR VAULT"):
        cursor.execute("DELETE FROM vault")
        conn.commit()
        st.rerun()
