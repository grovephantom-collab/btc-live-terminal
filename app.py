import time, hmac, threading, logging
from flask import Flask, request, jsonify, render_template_string
import config, database, market_data, smc_engine, trade_manager, notifications

log = logging.getLogger("Supervisor")
LOCK = threading.Lock()
STARTED = time.time()

STATE = {"status": "STARTING", "last_scan_epoch": 0, "last_signal": None, "last_error": None, "fencing_token": None}

def engine_supervision_loop():
    while True:
        try:
            is_leader, token = database.lease_tick()
            if not is_leader or token is None:
                STATE.update(status="STANDBY", fencing_token=None)
                time.sleep(2); continue

            STATE.update(status="RUNNING_PAPER", fencing_token=token)
            if not market_data.refresh_market_data(LOCK):
                STATE.update(last_error=market_data.MARKET.get("error"))
                time.sleep(config.SCAN_SECONDS); continue

            with LOCK:
                p = market_data.MARKET["price"]
                pts = market_data.MARKET["price_ts"]
                kts = market_data.MARKET["klines_ts"]
                ks = {k: list(v) for k, v in market_data.MARKET["klines"].items()}
                sess = market_data.MARKET["session"]
                poc = market_data.MARKET["poc"]

            # Stale Data Circuit Breaker
            if time.time() - pts > config.MAX_DATA_AGE or time.time() - kts > config.MAX_DATA_AGE:
                STATE["status"] = "DATA_STALE"
                time.sleep(config.SCAN_SECONDS); continue

            trade_manager.manage_positions(p, token)
            st = smc_engine.get_structure(ks)
            sig = smc_engine.evaluate_signal(st, ks["15m"], p, sess, poc)

            if sig:
                STATE["last_signal"] = {"direction": sig["direction"], "score": sig["score"], "setup": sig["setup_name"], "time": database.now_iso()}
                trade_manager.open_paper_trade(sig, token)

            STATE.update(last_scan_epoch=time.time(), last_error=None)
            time.sleep(config.SCAN_SECONDS)
        except Exception as e:
            STATE.update(status="ERROR", last_error=str(e)[:250])
            time.sleep(max(3, config.SCAN_SECONDS))

app = Flask(__name__)

# Authentication Gate
def auth_check() -> bool:
    h = request.headers.get("Authorization", "")
    return h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip().encode(), config.WORKER_AUTH_TOKEN.encode())

@app.before_request
def auth_gate():
    # Public views for monitor and UI
    if request.path in ("/healthz", "/", "/dashboard", "/api/public-stats"): 
        return None
    if not auth_check(): 
        return jsonify({"error": "unauthorized"}), 401

@app.get("/healthz")
def healthz():
    with LOCK:
        fresh = bool(market_data.MARKET["price"] and time.time() - market_data.MARKET["price_ts"] <= config.MAX_DATA_AGE)
    d = dict(STATE)
    d.update(mode="PAPER", symbol=config.SYMBOL, market_data_fresh=fresh, uptime_seconds=int(time.time() - STARTED), utc_time=database.now_iso())
    return jsonify(d), (200 if d["status"] in ("RUNNING_PAPER", "STANDBY") else 503)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SMC Institutional Terminal | BTCUSDT</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #090d16;
            --card: #111827;
            --border: #1f293d;
            --text-main: #f3f4f6;
            --text-dim: #9ca3af;
            --green: #10b981;
            --red: #ef4444;
            --accent: #38bdf8;
            --gold: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg);
            color: var(--text-main);
            font-family: 'JetBrains Mono', monospace;
            padding: 20px;
            font-size: 13px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 15px;
            margin-bottom: 20px;
        }
        .title { font-size: 18px; font-weight: 700; color: var(--accent); }
        .badge {
            background: rgba(16, 185, 129, 0.15);
            color: var(--green);
            padding: 4px 10px;
            border-radius: 4px;
            border: 1px solid var(--green);
            font-size: 11px;
            font-weight: 600;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 25px;
        }
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
        }
        .card-label { color: var(--text-dim); font-size: 11px; text-transform: uppercase; margin-bottom: 8px; }
        .card-value { font-size: 20px; font-weight: 700; }
        .active-box {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 25px;
        }
        .active-header {
            display: flex;
            justify-content: space-between;
            border-bottom: 1px solid var(--border);
            padding-bottom: 10px;
            margin-bottom: 15px;
            font-weight: 600;
        }
        .table-wrap {
            overflow-x: auto;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
        }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th, td { padding: 12px 16px; border-bottom: 1px solid var(--border); }
        th { color: var(--text-dim); font-size: 11px; text-transform: uppercase; background: #0c1220; }
        tr:hover { background: rgba(255, 255, 255, 0.02); }
        .long { color: var(--green); font-weight: 700; }
        .short { color: var(--red); font-weight: 700; }
        .pulse { animation: blink 1.5s infinite; }
        @keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <div class="title">BTCUSDT SMC TRADING TERMINAL</div>
            <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">Institutional Orderflow Vault</div>
        </div>
        <div class="badge pulse">● ENGINE ONLINE</div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-label">Bitcoin Price</div>
            <div class="card-value" style="color: var(--accent);">${{ "%.2f"|format(price) }}</div>
        </div>
        <div class="card">
            <div class="card-label">Dealing Session</div>
            <div class="card-value" style="font-size: 15px; color: var(--gold);">{{ session }}</div>
        </div>
        <div class="card">
            <div class="card-label">Realized Net PnL</div>
            <div class="card-value" style="color: {{ 'var(--green)' if pnl >= 0 else 'var(--red)' }};">
                {{ "%+.2f"|format(pnl) }} USD
            </div>
        </div>
        <div class="card">
            <div class="card-label">Win Rate / Trades</div>
            <div class="card-value">{{ win_rate }}% <span style="font-size: 13px; color: var(--text-dim);">({{ total_trades }}T)</span></div>
        </div>
    </div>

    <div class="active-box">
        <div class="active-header">
            <span>ACTIVE POSITION STATUS</span>
            <span style="color: var(--text-dim);">Max 1 Open Risk Cap</span>
        </div>
        {% if active %}
            <div style="display: flex; gap: 30px; flex-wrap: wrap;">
                <div><span style="color: var(--text-dim);">Side:</span> <span class="{{ active.direction.lower() }}">{{ active.direction }}</span></div>
                <div><span style="color: var(--text-dim);">Entry:</span> ${{ "%.2f"|format(active.entry_price) }}</div>
                <div><span style="color: var(--text-dim);">Dynamic SL:</span> ${{ "%.2f"|format(active.stop_price) }}</div>
                <div><span style="color: var(--text-dim);">TP1:</span> ${{ "%.2f"|format(active.tp1_price) }}</div>
                <div><span style="color: var(--text-dim);">Stage:</span> <span style="color: var(--gold);">{{ active.stage }}</span></div>
            </div>
        {% else %}
            <div style="color: var(--text-dim);">No active trade open. Strategy scanning closed 15M candles for Liquidity Sweeps & FVG CE mitigations.</div>
        {% endif %}
    </div>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Opened UTC</th>
                    <th>Setup Name</th>
                    <th>Side</th>
                    <th>Entry</th>
                    <th>Exit Reason</th>
                    <th>Net PnL (USD)</th>
                    <th>Weighted R</th>
                </tr>
            </thead>
            <tbody>
                {% for t in trades %}
                <tr>
                    <td>{{ t.opened_at_utc[11:19] }}</td>
                    <td>{{ t.setup_name }}</td>
                    <td class="{{ t.direction.lower() }}">{{ t.direction }}</td>
                    <td>${{ "%.1f"|format(t.entry_price) }}</td>
                    <td>{{ t.close_reason or 'IN PROGRESS' }}</td>
                    <td style="color: {{ 'var(--green)' if (t.realized_pnl_usd or 0) > 0 else 'var(--red)' if (t.realized_pnl_usd or 0) < 0 else 'var(--text-dim)' }};">
                        {{ "%+.2f"|format(t.realized_pnl_usd or 0) }}
                    </td>
                    <td>{{ "%+.2f"|format(t.weighted_pnl_r or 0) }}R</td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="7" style="text-align: center; color: var(--text-dim); padding: 30px;">
                        No past trades logged yet. Scanner is standing by for setup execution.
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

    <script>
        // Auto-refresh screen every 12 seconds
        setTimeout(() => { window.location.reload(); }, 12000);
    </script>
</body>
</html>
"""

# Public Professional GUI Dashboard
@app.get("/")
@app.get("/dashboard")
def dashboard_gui():
    c = database.db()
    try:
        ts = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 25").fetchall()]
        act = [dict(r) for r in c.execute("SELECT * FROM trades WHERE stage!='CLOSED' ORDER BY id DESC LIMIT 1").fetchall()]
        pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()["p"] or 0.0)
        
        with LOCK:
            cp = float(market_data.MARKET.get("price") or 0.0)
            cs = str(market_data.MARKET.get("session") or "OFFLINE")
            
        closed = [t for t in ts if t.get("stage") == "CLOSED"]
        wins = sum(1 for t in closed if float(t.get("realized_pnl_usd") or 0) > 0)
        total = len(closed)
        win_rate = round((wins / total * 100), 1) if total > 0 else 0.0

        return render_template_string(
            HTML_TEMPLATE,
            price=cp,
            session=cs,
            pnl=pnl,
            win_rate=win_rate,
            total_trades=total,
            active=act[0] if act else None,
            trades=ts
        )
    finally:
        c.close()

# Token Protected Vault Endpoint (API)
@app.get("/vault-data")
def vault_data():
    limit = min(max(int(request.args.get("limit", "50")), 1), 200)
    c = database.db()
    try:
        ts = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        act = [dict(r) for r in c.execute("SELECT * FROM trades WHERE stage!='CLOSED' ORDER BY id DESC LIMIT 1").fetchall()]
        pnl = float(c.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) p FROM trades").fetchone()["p"] or 0.0)
        with LOCK:
            cp, cs, cpoc = market_data.MARKET.get("price") or 0.0, market_data.MARKET.get("session") or "UNKNOWN", market_data.MARKET.get("poc") or 0.0
        return jsonify({
            "mode": "PAPER", "symbol": config.SYMBOL, "equity_estimate_usd": trade_manager.get_equity(c),
            "realized_pnl_usd": pnl, "market": {"price": cp, "session": cs, "poc": cpoc},
            "active_trade": act[0] if act else None, "trades": ts, "history": ts
        })
    finally: c.close()

if __name__ == "__main__":
    database.init_db()
    threading.Thread(target=notifications.telegram_worker_loop, daemon=True, name="tg_thread").start()
    threading.Thread(target=engine_supervision_loop, daemon=True, name="engine_thread").start()
    log.info(f"Engine cluster online on port {config.PORT}")
    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True, use_reloader=False)
