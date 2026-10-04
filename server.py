import asyncio
import json
import sqlite3
import aiohttp
import requests
from datetime import datetime

BOT_TOKEN = "8941403990:AAGLH_dupqmGoipglhVvRuiPBzvgMqJR3Ms"
CHAT_ID = "7886716805"

DB_FILE = "trades_vault.db"

# Database initialization
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS vault (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            direction TEXT,
            entry REAL,
            sweep_level REAL,
            atr REAL,
            sl REAL,
            tp REAL,
            exit REAL,
            result TEXT,
            pnl TEXT,
            is_win INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def save_vault(trade, exit_price, result, pnl, is_win):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO vault (timestamp, direction, entry, sweep_level, atr, sl, tp, exit, result, pnl, is_win)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        trade['time'], trade['dir'], trade['entry'], trade['sweep_level'],
        trade['atr'], trade['sl'], trade['tp'], exit_price, result, pnl, is_win
    ))
    conn.commit()
    conn.close()

# 48H Historical Buffer Fetcher
def fetch_initial_candles():
    url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit=650"
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
        if isinstance(r, list) and len(r) >= 576:
            return [{"time": int(b[0]/1000), "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4]), "vol": float(b[5])} for b in r]
    except Exception as e:
        print(f"Fetch error: {e}")
    return []

# True Range ATR(14)
def calculate_atr(data, period=14):
    if len(data) < period + 1:
        return 85.0
    trs = []
    for i in range(len(data) - period, len(data)):
        hl = data[i]['high'] - data[i]['low']
        hc = abs(data[i]['high'] - data[i - 1]['close'])
        lc = abs(data[i]['low'] - data[i - 1]['close'])
        trs.append(max(hl, hc, lc))
    return sum(trs) / period

def get_48h_bounds(data):
    slice_576 = data[-576:]
    h48 = max(c['high'] for c in slice_576)
    l48 = min(c['low'] for c in slice_576)
    return h48, l48

# Global Strategy State
state = "SCANNING"  # SCANNING | SWEEP_LOW_WAIT | SWEEP_HIGH_WAIT | ACTIVE_TRADE
sweep_extreme = None
sweep_level = None
sweep_candle_time = None
active_trade = None
candles_data = []

def process_closed_candle(closed_candle):
    global state, sweep_extreme, sweep_level, sweep_candle_time, active_trade, candles_data
    candles_data.append(closed_candle)
    if len(candles_data) > 650:
        candles_data.pop(0)

    completed_buffer = candles_data[:-1]
    h48, l48 = get_48h_bounds(completed_buffer)
    atr = calculate_atr(completed_buffer, 14)

    # FALSE SIGNAL SHIELD RULES
    if state == "SCANNING":
        if closed_candle['low'] < l48:
            state = "SWEEP_LOW_WAIT"
            sweep_extreme = closed_candle['low']
            sweep_level = l48
            sweep_candle_time = closed_candle['time']
            print(f"[SHIELD] 48H Low Swept: {l48:.1f}. Waiting for next candle reclaim.")
        elif closed_candle['high'] > h48:
            state = "SWEEP_HIGH_WAIT"
            sweep_extreme = closed_candle['high']
            sweep_level = h48
            sweep_candle_time = closed_candle['time']
            print(f"[SHIELD] 48H High Swept: {h48:.1f}. Waiting for next candle rejection.")

    elif state == "SWEEP_LOW_WAIT":
        if closed_candle['time'] > sweep_candle_time:
            if closed_candle['close'] > sweep_level:
                # Confirmed Reclaim
                entry = closed_candle['close']
                sl = sweep_extreme - (1.6 * atr)
                risk = entry - sl
                tp = entry + (2.5 * risk)
                arm_trade("LONG", entry, sl, tp, sweep_level, atr)
            else:
                if closed_candle['low'] < sweep_extreme:
                    sweep_extreme = closed_candle['low']
                else:
                    state = "SCANNING"
                    print("[SHIELD] Failed Reclaim -> Back to SCANNING.")

    elif state == "SWEEP_HIGH_WAIT":
        if closed_candle['time'] > sweep_candle_time:
            if closed_candle['close'] < sweep_level:
                # Confirmed Rejection
                entry = closed_candle['close']
                sl = sweep_extreme + (1.6 * atr)
                risk = sl - entry
                tp = entry - (2.5 * risk)
                arm_trade("SHORT", entry, sl, tp, sweep_level, atr)
            else:
                if closed_candle['high'] > sweep_extreme:
                    sweep_extreme = closed_candle['high']
                else:
                    state = "SCANNING"
                    print("[SHIELD] Failed Rejection -> Back to SCANNING.")

def arm_trade(dir_type, entry, sl, tp, s_level, atr_val):
    global state, active_trade
    state = "ACTIVE_TRADE"
    t_str = datetime.now().strftime('%H:%M:%S')
    active_trade = {
        "dir": dir_type,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "sweep_level": s_level,
        "atr": atr_val,
        "time": t_str
    }
    
    msg = (
        f"🚨 <b>BTCUSDT {dir_type} PRE-SIGNAL ARMED</b> 🚨\n\n"
        f"⏱ <b>Timeframe:</b> 5M (48H Sweep)\n"
        f"🎯 <b>Basis:</b> 48H {'Low Reclaim' if dir_type == 'LONG' else 'High Rejection'} (Closed Candle)\n\n"
        f"🔹 <b>Entry:</b> ${entry:.2f}\n"
        f"🛑 <b>Stop Loss:</b> ${sl:.2f}\n"
        f"🎯 <b>Take Profit (2.5R):</b> ${tp:.2f}\n"
        f"⚡ <b>RR Ratio:</b> 1:2.5\n"
        f"🛡 <b>ATR(14):</b> {atr_val:.1f}\n\n"
        f"<i>Monitoring real-time ticks for resolution...</i>"
    )
    send_telegram(msg)
    print(f"[TRADE ARMED] {dir_type} @ {entry} | SL: {sl} | TP: {tp}")

def resolve_trade(result, pnl, is_win, exit_price):
    global state, active_trade
    save_vault(active_trade, exit_price, result, pnl, is_win)
    
    msg = (
        f"🏁 <b>TRADE RESOLVED: {result}</b>\n\n"
        f"📌 <b>Direction:</b> {active_trade['dir']}\n"
        f"🔹 <b>Entry:</b> ${active_trade['entry']:.2f}\n"
        f"🔸 <b>Exit Price:</b> ${exit_price:.2f}\n"
        f"💰 <b>Result:</b> {pnl}\n\n"
        f"🔄 <i>State Reset: SCANNING for next 48H institutional pool...</i>"
    )
    send_telegram(msg)
    print(f"[RESOLVED] {result} exit @ {exit_price}")

    state = "SCANNING"
    active_trade = None

# WebSocket Tasks
async def kline_listener():
    url = "wss://fstream.binance.com/ws/btcusdt@kline_5m"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url) as ws:
                    print("Connected to Binance Futures 5M Kline Stream.")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            k = data.get('k', {})
                            if k.get('x'):  # Closed 5M Candle Only
                                closed = {
                                    "time": int(k['t'] / 1000),
                                    "open": float(k['o']),
                                    "high": float(k['h']),
                                    "low": float(k['l']),
                                    "close": float(k['c']),
                                    "vol": float(k['v'])
                                }
                                process_closed_candle(closed)
        except Exception as e:
            print(f"Kline WS reconnecting: {e}")
            await asyncio.sleep(2)

async def trade_listener():
    global state, active_trade
    url = "wss://fstream.binance.com/ws/btcusdt@trade"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url) as ws:
                    print("Connected to Binance Futures Real-Time Trade Stream.")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            price = float(data.get('p', 0.0))
                            if state == "ACTIVE_TRADE" and active_trade:
                                if active_trade['dir'] == "LONG":
                                    if price >= active_trade['tp']:
                                        resolve_trade("TP HIT 🔥", "+2.5R", 1, price)
                                    elif price <= active_trade['sl']:
                                        resolve_trade("SL HIT 🛑", "-1R", 0, price)
                                elif active_trade['dir'] == "SHORT":
                                    if price <= active_trade['tp']:
                                        resolve_trade("TP HIT 🔥", "+2.5R", 1, price)
                                    elif price >= active_trade['sl']:
                                        resolve_trade("SL HIT 🛑", "-1R", 0, price)
        except Exception as e:
            print(f"Trade WS reconnecting: {e}")
            await asyncio.sleep(2)

async def main():
    global candles_data
    init_db()
    print("Fetching 48H buffer...")
    candles_data = fetch_initial_candles()
    if not candles_data:
        print("Failed to fetch initial candles. Exiting.")
        return
    print(f"Loaded {len(candles_data)} candles into buffer.")
    send_telegram("🚀 <b>BTCUSDT 48H RADAR BACKGROUND WORKER ACTIVATED</b>\nMonitoring 24/7 Binance Futures streams...")
    
    await asyncio.gather(
        kline_listener(),
        trade_listener()
    )

if __name__ == "__main__":
    asyncio.run(main())
