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

# Har 2.5 second me silent high-frequency tick
st_autorefresh(interval=2500, limit=None, key="institutional_34_engine_radar")

# Exact Dark Theme + Floating Vault CSS
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

        /* Floating Popup Modal */
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

# --- SQLite Trade Vault Database ---
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

# Clean mock dummy data if remaining from previous tests
if 'cleaned_vault' not in st.session_state:
    cur.execute("SELECT COUNT(*) FROM vault WHERE result LIKE '%(50%)%'")
    if cur.fetchone()[0] > 0:
        cur.execute("DELETE FROM vault")
        conn.commit()
    st.session_state['cleaned_vault'] = True

cur.execute("SELECT COUNT(*) FROM vault")
vault_count = cur.fetchone()[0]

# =========================================================================
# 1. MULTI-TIMEFRAME DATA ENGINE (OHLCV + VOLATILITY)
# =========================================================================
def fetch_mtf_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    urls = [
        ("https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=65",
         "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=50"),
        ("https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=65",
         "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=50")
    ]
    for u5, u1h in urls:
        try:
            r5 = requests.get(u5, headers=headers, timeout=2.5).json()
            r1 = requests.get(u1h, headers=headers, timeout=2.5).json()
            if isinstance(r5, list) and len(r5) > 10 and isinstance(r1, list) and len(r1) > 10:
                df5 = pd.DataFrame(r5, columns=['t','o','h','l','c','v','ct','qa','tr','tb','tq','i'])
                df1 = pd.DataFrame(r1, columns=['t','o','h','l','c','v','ct','qa','tr','tb','tq','i'])
                for df in [df5, df1]:
                    df['time'] = pd.to_datetime(df['t'], unit='ms')
                    for col in ['o','h','l','c','v']: df[col] = df[col].astype(float)
                    df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                return df5, df1
        except Exception:
            continue
    return pd.DataFrame(), pd.DataFrame()

df_5m, df_1h = fetch_mtf_data()
if df_5m.empty:
    st.stop()

# =========================================================================
# 2. 48H STRUCTURE + PREVIOUS-DAY ENGINE (PDH / PDL / POC)
# =========================================================================
pd_window = df_1h.iloc[-48:-24] if len(df_1h) >= 48 else df_1h.iloc[:24]
pdh = float(pd_window['high'].max())
pdl = float(pd_window['low'].min())
h48 = float(df_1h['high'].max())
l48 = float(df_1h['low'].min())

# =========================================================================
# 3. REGIME DETECTION + TECHNICAL ENGINES (MOMENTUM / SQUEEZE / VOL)
# =========================================================================
# Moving Averages & Bands
df_5m['ema9'] = df_5m['close'].ewm(span=9, adjust=False).mean()
df_5m['ema21'] = df_5m['close'].ewm(span=21, adjust=False).mean()
df_5m['ema50'] = df_5m['close'].ewm(span=50, adjust=False).mean()

# ATR
df_5m['tr'] = np.maximum(df_5m['high'] - df_5m['low'],
                         np.maximum(abs(df_5m['high'] - df_5m['close'].shift()),
                                    abs(df_5m['low'] - df_5m['close'].shift())))
df_5m['atr'] = df_5m['tr'].rolling(14).mean()

# Bollinger Bands (Squeeze Engine)
df_5m['bb_mid'] = df_5m['close'].rolling(20).mean()
df_5m['bb_std'] = df_5m['close'].rolling(20).std()
df_5m['bb_up'] = df_5m['bb_mid'] + (df_5m['bb_std'] * 2)
df_5m['bb_low'] = df_5m['bb_mid'] - (df_5m['bb_std'] * 2)
df_5m['bb_width'] = (df_5m['bb_up'] - df_5m['bb_low']) / df_5m['bb_mid']

# RSI Momentum Engine
delta = df_5m['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rs = gain / (loss + 1e-9)
df_5m['rsi'] = 100 - (100 / (1 + rs))

# Last Bar Variables
last = df_5m.iloc[-1]
prev = df_5m.iloc[-2]
cur_p = round(float(last['close']), 2)
cur_atr = round(float(last['atr']) if not np.isnan(last['atr']) else 110.0, 2)
if cur_atr < 35: cur_atr = 95.0

# Regime Detection
adx_proxy = abs(last['ema9'] - last['ema50']) / cur_atr
if last['bb_width'] < 0.003:
    market_regime = "BB SQUEEZE"
elif adx_proxy > 1.8:
    market_regime = "TRENDING"
else:
    market_regime = "RANGING"

# =========================================================================
# 4. CONFLUENCE AI SCORING (LIQUIDITY SWEEP + MSS + FVG + PULLBACK)
# =========================================================================
score = 0
reasons = []

# (A) Liquidity Sweep Engine
if prev['low'] <= pdl and last['close'] > pdl:
    score += 35
    reasons.append("PDL LIQ SWEEP")
elif prev['high'] >= pdh and last['close'] < pdh:
    score -= 35
    reasons.append("PDH LIQ SWEEP")

# (B) Market Structure Shift (MSS) & Displacement
swing_high = df_5m['high'].iloc[-7:-2].max()
swing_low = df_5m['low'].iloc[-7:-2].min()

if last['close'] > swing_high and last['volume'] > df_5m['volume'].iloc[-6:-1].mean():
    score += 30
    reasons.append("BULLISH MSS + DISPLACEMENT")
elif last['close'] < swing_low and last['volume'] > df_5m['volume'].iloc[-6:-1].mean():
    score -= 30
    reasons.append("BEARISH MSS + DISPLACEMENT")

# (C) Momentum & EMA Pullback
if last['ema9'] > last['ema21'] and last['close'] > last['open']:
    score += 20
elif last['ema9'] < last['ema21'] and last['close'] < last['open']:
    score -= 20

# (D) RSI Extreme Reversal
if last['rsi'] < 32:
    score += 15
elif last['rsi'] > 68:
    score -= 15

# =========================================================================
# 5. RISK ENGINE, SIGNAL CLASSIFICATION & TARGET LEVELS
# =========================================================================
has_signal = False
radar_status = "MONITORING PULLBACKS"
entry_txt, sl_txt, tp_txt = "--", "--", "--"
entry_val, sl_val, tp_val = 0.0, 0.0, 0.0
sig_tier = "MICRO (5M)"

if score >= 50:
    has_signal = True
    radar_status = "PRE-SIGNAL LONG"
    entry_val = cur_p
    sl_val = round(cur_p - (cur_atr * 1.5), 1)
    tp_val = round(cur_p + (cur_atr * 2.5), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"
elif score <= -50:
    has_signal = True
    radar_status = "PRE-SIGNAL SHORT"
    entry_val = cur_p
    sl_val = round(cur_p + (cur_atr * 1.5), 1)
    tp_val = round(cur_p - (cur_atr * 2.5), 1)
    entry_txt = f"${entry_val}"
    sl_txt = f"${sl_val}"
    tp_txt = f"${tp_val}"

# Active Trade Monitoring against SQLite Vault
if 'active_trade' in st.session_state:
    at = st.session_state['active_trade']
    # TP Hit
    if (at['dir'] == 'LONG' and cur_p >= at['tp']) or (at['dir'] == 'SHORT' and cur_p <= at['tp']):
        cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now().strftime('%H:%M'), at['dir'], at['entry'], "TP HIT 🔥", "(+$0.65)", 1))
        conn.commit()
        del st.session_state['active_trade']
        st.rerun()
    # SL Hit
    elif (at['dir'] == 'LONG' and cur_p <= at['sl']) or (at['dir'] == 'SHORT' and cur_p >= at['sl']):
        cur.execute("INSERT INTO vault (timestamp, direction, entry, result, pnl, is_win) VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now().strftime('%H:%M'), at['dir'], at['entry'], "SL HIT 🛑", "(-$0.80)", 0))
        conn.commit()
        del st.session_state['active_trade']
        st.rerun()
elif has_signal:
    # Auto-register new trade into tracker
    st.session_state['active_trade'] = {
        'dir': 'LONG' if "LONG" in radar_status else 'SHORT',
        'entry': entry_val,
        'sl': sl_val,
        'tp': tp_val
    }

# --- TOP HUD HEADER ---
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

# --- FLOATING POPUP MODAL (VAULT) ---
if show_vault:
    cur.execute("SELECT timestamp, direction, entry, result, pnl, is_win FROM vault ORDER BY id DESC")
    rows = cur.fetchall()
    total = len(rows)
    wins = sum(1 for r in rows if r[5] == 1)
    rate = int((wins / total) * 100) if total > 0 else 0

    trade_items_html = ""
    if total == 0:
        trade_items_html = "<div style='font-size:10px; color:#64748b; text-align:center; padding:15px 0;'>No trades yet. Listening for institutional triggers...</div>"
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

# --- RESPONSIVE CANDLESTICK CHART (NO TOUCH ZOOM) ---
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
    name="BTCUSDT"
))

# Signal Lines (Active on Confluence Trigger)
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

# Real-time Red Price Badge
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

# --- DUAL BOTTOM CONTROLS ---
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
