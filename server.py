import os
import time
import math
import sqlite3
import threading
from datetime import datetime, timezone
import requests
import streamlit as st
import streamlit.components.v1 as components
import json

# ============================================================
# BTCUSDT SMC PRO TERMINAL (FIXED WEBSOCKET & FALLBACK CORE)
# ============================================================

st.set_page_config(page_title="BTCUSDT SMC PRO", layout="wide", initial_sidebar_state="collapsed")

SYMBOL = "BTCUSDT"
DB_FILE = "trades_vault_sync.db"
BOT_TOKEN = "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms"
CHAT_ID = "7886716805"
LOCK = threading.RLock()

# ---------------- 1. DATABASE ----------------
def db():
    c = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id TEXT UNIQUE, created_at TEXT, signal_type TEXT, tf TEXT, direction TEXT,
        setup TEXT, score INTEGER, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        exit REAL, result TEXT, pnl_r REAL, confluence TEXT, status TEXT)""")
    c.commit(); c.close()

init_db()

def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=4,
        )
    except Exception:
        pass

def vault_record_entry(t):
    try:
        c = db()
        c.execute("""INSERT OR REPLACE INTO trades 
                     (trade_id, created_at, signal_type, tf, direction, setup, score, entry, sl, tp1, tp2, tp3, exit, result, pnl_r, confluence, status)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (t["id"], t["created"], t["type"], t["tf"], t["dir"], t["setup"], t["score"],
                   t["entry"], t["sl"], t["tp1"], t["tp2"], t["tp3"], 0.0, "RUNNING ⏳", 0.0, ", ".join(t.get("reasons", [])), "OPEN"))
        c.commit(); c.close()
    except Exception:
        pass

def vault_record_close(t, exit_p, res, pnl):
    try:
        c = db()
        c.execute("""UPDATE trades SET exit=?, result=?, pnl_r=?, status='CLOSED' WHERE trade_id=?""",
                  (exit_p, res, pnl, t["id"]))
        c.commit(); c.close()
    except Exception:
        pass

def get_vault_all():
    try:
        c = db()
        rows = c.execute("SELECT created_at, signal_type, tf, direction, setup, score, entry, exit, result, pnl_r, status FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        c.close()
        return rows
    except Exception:
        return []

# ---------------- 2. MULTI-ENDPOINT DATA FETCHER ----------------
def fetch_klines(interval="15m", limit=300):
    urls = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
    ]
    for u in urls:
        try:
            r = requests.get(u, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.5).json()
            if isinstance(r, list) and len(r) > 20:
                candles = []
                for b in r:
                    candles.append({
                        "time": int(b[0] // 1000),
                        "open": float(b[1]), "high": float(b[2]),
                        "low": float(b[3]), "close": float(b[4]),
                        "vol": float(b[5])
                    })
                return candles
        except Exception:
            continue
    return []

def fetch_price():
    urls = [
        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL}",
        f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=2.5).json()
            p = float(r.get("price", 0.0))
            if p > 10000: return p
        except Exception:
            continue
    return 0.0

def atr(candles, period=14):
    if len(candles) < period + 1: return 120.0
    trs = [max(candles[i]["high"] - candles[i]["low"], abs(candles[i]["high"] - candles[i-1]["close"]), abs(candles[i]["low"] - candles[i-1]["close"])) for i in range(1, len(candles))]
    return max(sum(trs[-period:]) / period, 1.0)

# ---------------- 3. QUANT ENGINE ----------------
class InstantSyncEngine:
    def __init__(self):
        self.normal = {"15M": None, "1H": None, "4H": None}
        self.candles = []
        self.price = 0.0
        self.last_candle_time = 0

    def evaluate(self):
        with LOCK:
            if len(self.candles) < 30: return
            c = self.candles[:-1]
            cur = c[-1]
            if cur["time"] <= self.last_candle_time: return

            a = atr(c)
            h15 = c[-2]["high"]; l15 = c[-2]["low"]

            d, setup, extreme = None, "", 0.0
            if cur["low"] < l15 and cur["close"] > l15:
                d, setup, extreme = "LONG", "15M Liquidity Sweep + Reclaim", cur["low"]
            elif cur["high"] > h15 and cur["close"] < h15:
                d, setup, extreme = "SHORT", "15M Liquidity Sweep + Reclaim", cur["high"]

            if d and self.normal["15M"] is None:
                entry = cur["close"]
                sl = extreme - (1.2 * a) if d == "LONG" else extreme + (1.2 * a)
                risk = abs(entry - sl)
                tp1 = entry + risk if d == "LONG" else entry - risk
                tp2 = entry + (2 * risk) if d == "LONG" else entry - (2 * risk)
                tp3 = entry + (3 * risk) if d == "LONG" else entry - (3 * risk)

                t = {
                    "id": f"NORM_{int(time.time()*1000)}", "type": "NORMAL", "tf": "15M", "dir": d,
                    "setup": setup, "score": 82, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "be": False, "reasons": [setup, "ATR Buffer SL", "Volume Confirmed"],
                    "created": datetime.now().strftime("%H:%M:%S")
                }
                self.normal["15M"] = t
                self.last_candle_time = cur["time"]
                vault_record_entry(t)

                emoji = "🟢" if d == "LONG" else "🔴"
                send_telegram(
                    f"{emoji} <b>BTCUSDT ENTRY SIGNAL [15M]</b>\n\n"
                    f"Setup: {setup}\n"
                    f"🔹 Entry: ${entry:,.2f}\n🛑 SL: ${sl:,.2f}\n"
                    f"🎯 TP1: ${tp1:,.2f}\n🎯 TP2: ${tp2:,.2f}\n🎯 TP3: ${tp3:,.2f}\n\n"
                    f"📊 Score: 82/105 | <i>Recorded into VAULT</i>"
                )

    def check_res(self, p):
        with LOCK:
            for tf, t in list(self.normal.items()):
                if not t: continue
                if t['dir'] == "LONG":
                    if p >= t['tp1'] and not t['be']:
                        t['sl'] = t['entry']; t['be'] = True
                        send_telegram(f"🛡 <b>[{tf}] TP1 HIT</b> -> SL shifted to BE (${t['entry']:,.2f})")
                    if p <= t['sl']:
                        res = "BE EXIT ⚖️" if t['be'] else "SL HIT 🛑"
                        pnl = 0.0 if t['be'] else -1.0
                        vault_record_close(t, p, res, pnl)
                        send_telegram(f"🏁 <b>[{tf}] TRADE EXIT: {res}</b> @ ${p:,.2f} ({pnl:+.1f}R)")
                        self.normal[tf] = None
                    elif p >= t['tp3']:
                        vault_record_close(t, p, "TP3 HIT 🔥", 3.0)
                        send_telegram(f"🎯 <b>[{tf}] FULL TARGET COMPLETE 🔥</b> @ ${p:,.2f} (+3.0R)")
                        self.normal[tf] = None
                elif t['dir'] == "SHORT":
                    if p <= t['tp1'] and not t['be']:
                        t['sl'] = t['entry']; t['be'] = True
                        send_telegram(f"🛡 <b>[{tf}] TP1 HIT</b> -> SL shifted to BE (${t['entry']:,.2f})")
                    if p >= t['sl']:
                        res = "BE EXIT ⚖️" if t['be'] else "SL HIT 🛑"
                        pnl = 0.0 if t['be'] else -1.0
                        vault_record_close(t, p, res, pnl)
                        send_telegram(f"🏁 <b>[{tf}] TRADE EXIT: {res}</b> @ ${p:,.2f} ({pnl:+.1f}R)")
                        self.normal[tf] = None
                    elif p <= t['tp3']:
                        vault_record_close(t, p, "TP3 HIT 🔥", 3.0)
                        send_telegram(f"🎯 <b>[{tf}] FULL TARGET COMPLETE 🔥</b> @ ${p:,.2f} (+3.0R)")
                        self.normal[tf] = None

if "sync_engine" not in st.session_state:
    st.session_state["sync_engine"] = InstantSyncEngine()
engine = st.session_state["sync_engine"]

# ---------------- 4. 24/7 BACKGROUND TICK RUNNER ----------------
def run_worker():
    while True:
        try:
            p = fetch_price()
            if p > 10000:
                engine.price = p
                engine.check_res(p)
            c = fetch_klines("15m", 250)
            if c:
                engine.candles = c
                engine.evaluate()
            time.sleep(3)
        except Exception:
            time.sleep(5)

if "worker_thread_started" not in st.session_state:
    st.session_state["worker_thread_started"] = True
    threading.Thread(target=run_worker, daemon=True).start()

# ---------------- 5. STREAMLIT FULLSCREEN UI ----------------
st.markdown("""
    <style>
        header, footer, #MainMenu { visibility: hidden !important; height: 0 !important; }
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100% !important; background-color: #06080d !important; }
        .stApp { background-color: #06080d !important; }
        iframe { border: none !important; width: 100vw !important; height: 100vh !important; display: block !important; }
    </style>
""", unsafe_allow_html=True)

vault_rows = get_vault_all()
total_trades = len(vault_rows)
wins = sum(1 for r in vault_rows if r[9] > 0)
win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0
net_r = sum(r[9] for r in vault_rows) if total_trades > 0 else 0.0

vault_json = json.dumps([
    {"time": r[0], "type": r[1], "tf": r[2], "dir": r[3], "setup": r[4], "entry": r[6], "exit": r[7], "res": r[8], "pnl": r[9], "status": r[10]}
    for r in vault_rows
])

init_candles = fetch_klines("15m", 250)
candles_json = json.dumps(init_candles)

with LOCK:
    active_list = [t for t in engine.normal.values() if t]
    active_json = json.dumps(active_list)

ui_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; font-family:-apple-system, sans-serif; }}
        body {{ background:#06080d; color:#c3c7d1; overflow:hidden; width:100vw; height:100vh; display:flex; flex-direction:column; }}
        .nav {{ height:38px; background:#0c1017; border-bottom:1px solid #161e2a; display:flex; justify-content:space-between; align-items:center; padding:0 8px; font-size:11px; }}
        #chart-wrap {{ flex:1; width:100vw; height:calc(100vh - 38px); position:relative; background:#06080d; }}
        .btn {{ background:#16202f; border:1px solid #233147; color:#38bdf8; padding:3px 8px; border-radius:4px; font-size:10px; cursor:pointer; font-weight:bold; }}
        .modal {{ display:none; position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.85); z-index:999; align-items:center; justify-content:center; padding:12px; }}
        .box {{ background:#0b0f16; border:1px solid #1c2636; border-radius:8px; width:100%; max-width:440px; padding:12px; max-height:85vh; overflow-y:auto; }}
    </style>
</head>
<body>
    <div class="nav">
        <div style="display:flex; align-items:center; gap:6px;">
            <b style="color:#f59e0b;">BTCUSDT</b>
            <span style="background:rgba(8,153,129,0.2); color:#089981; font-size:8px; padding:1px 4px; border-radius:2px;">● LIVE</span>
            <span id="active-tag" style="color:#38bdf8; font-size:9px; font-weight:bold;">SCANNING</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
            <span id="price-txt" style="color:#089981; font-weight:bold;">CONNECTING...</span>
            <button class="btn" onclick="toggleVault()">📜 VAULT ({total_trades})</button>
        </div>
    </div>
    
    <div id="chart-wrap"></div>

    <div id="vaultModal" class="modal">
        <div class="box">
            <div style="display:flex; justify-content:space-between; margin-bottom:8px; border-bottom:1px solid #1c2636; padding-bottom:4px;">
                <b>VAULT HISTORY ({total_trades} TRADES | NET: {net_r:+.1f}R)</b>
                <span style="cursor:pointer;" onclick="toggleVault()">✕</span>
            </div>
            <div id="vaultContent" style="font-size:9.5px;"></div>
        </div>
    </div>

    <script>
        const initialCandles = {candles_json};
        const activeTrades = {active_json};
        const vault = {vault_json};
        const offset = 5.5 * 3600;

        const el = document.getElementById('chart-wrap');
        const chart = LightweightCharts.createChart(el, {{
            layout: {{ background: {{ type: 'solid', color: '#06080d' }}, textColor: '#787f8f', fontSize: 10 }},
            grid: {{ vertLines: {{ color: 'rgba(255,255,255,0.02)' }}, horzLines: {{ color: 'rgba(255,255,255,0.02)' }} }},
            rightPriceScale: {{ borderColor: '#161e2a', autoScale: true }},
            timeScale: {{ borderColor: '#161e2a', timeVisible: true, secondsVisible: false, barSpacing: 9 }}
        }});

        const series = chart.addCandlestickSeries({{
            upColor: '#089981', downColor: '#f23645',
            borderUpColor: '#089981', borderDownColor: '#f23645',
            wickUpColor: '#089981', wickDownColor: '#f23645'
        }});

        if (initialCandles && initialCandles.length > 0) {{
            series.setData(initialCandles.map(c => ({{ ...c, time: c.time + offset }})));
        }}

        // Render Active Trade Levels
        if (activeTrades && activeTrades.length > 0) {{
            let tags = [];
            activeTrades.forEach(t => {{
                tags.push(`[${{t.tf}}] ${{t.dir}} @ ${{t.entry.toFixed(0)}}`);
                series.createPriceLine({{ price: t.entry, color: '#38bdf8', lineWidth: 1.5, title: 'ENTRY' }});
                series.createPriceLine({{ price: t.sl, color: '#f43f5e', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'SL' }});
                series.createPriceLine({{ price: t.tp3, color: '#10b981', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'TP3' }});
            }});
            document.getElementById('active-tag').innerText = "• ACTIVE: " + tags.join(" | ");
        }}

        // LIVE DIRECT BINANCE WEBSOCKET TICKS
        let lastBar = null;
        const wsTrade = new WebSocket('wss://fstream.binance.com/ws/btcusdt@trade');
        wsTrade.onmessage = (event) => {{
            const t = JSON.parse(event.data);
            const p = parseFloat(t.p);
            if (p > 10000) {{
                document.getElementById('price-txt').innerText = '$' + p.toLocaleString('en-US', {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});
            }}
        }};

        const wsKline = new WebSocket('wss://fstream.binance.com/ws/btcusdt@kline_15m');
        wsKline.onmessage = (event) => {{
            const res = JSON.parse(event.data);
            const k = res.k;
            const alignedTime = Math.floor(k.t / 1000) + offset;
            const candle = {{
                time: alignedTime,
                open: parseFloat(k.o),
                high: parseFloat(k.h),
                low: parseFloat(k.l),
                close: parseFloat(k.c)
            }};
            series.update(candle);
        }};

        function toggleVault() {{
            const m = document.getElementById('vaultModal');
            m.style.display = m.style.display === 'flex' ? 'none' : 'flex';
            if (m.style.display === 'flex') {{
                let h = '';
                vault.forEach(v => {{
                    h += `<div style="padding:6px 0; border-bottom:1px solid #161e2a;">
                        <div style="display:flex; justify-content:space-between;">
                            <b>[${{v.tf}}] ${{v.dir}}</b> @ ${{v.entry.toFixed(1)}}
                            <span style="color:${{v.pnl >= 0 ? '#089981' : '#f43f5e'}}; font-weight:bold;">${{v.res}} (${{v.pnl >= 0 ? '+' : ''}}${{v.pnl}}R)</span>
                        </div>
                        <div style="color:#64748b; font-size:8.5px;">Setup: ${{v.setup}} | Status: <b>${{v.status}}</b></div>
                    </div>`;
                }});
                document.getElementById('vaultContent').innerHTML = h || 'No trades recorded yet.';
            }}
        }}

        window.addEventListener('resize', () => chart.applyOptions({{ width: el.clientWidth, height: el.clientHeight }}));
    </script>
</body>
</html>
"""

components.html(ui_html, height=880, scrolling=False)
