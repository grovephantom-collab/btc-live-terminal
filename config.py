import os, sys, math, sqlite3

def required(name: str, n: int = 24, fallback: str = "") -> str:
    v = os.getenv(name, fallback).strip()
    if len(v) < n:
        raise RuntimeError(f"{name} is required and must be at least {n} characters")
    return v

def fenv(name: str, default: float, minimum: float = 0.0) -> float:
    v = float(os.getenv(name, str(default)))
    if not math.isfinite(v) or v < minimum:
        raise RuntimeError(f"{name} must be finite and >= {minimum}")
    return v

def ienv(name: str, default: int, minimum: int = 0) -> int:
    v = int(os.getenv(name, str(default)))
    if v < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return v

def resolve_db_path(filename: str = "institutional_vault.db") -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [os.path.join(base_dir, filename), os.path.join("/tmp", filename)]:
        try:
            conn = sqlite3.connect(candidate, timeout=2.0)
            conn.execute("CREATE TABLE IF NOT EXISTS _probe (id INT);")
            conn.execute("DROP TABLE _probe;")
            conn.commit()
            conn.close()
            return candidate
        except Exception:
            continue
    return os.path.join("/tmp", filename)

try:
    API_SECRET_KEY = required("API_SECRET_KEY", 24, "institutional_sec_key_1234567890")
    WORKER_AUTH_TOKEN = required("WORKER_AUTH_TOKEN", 24, "worker_auth_token_secret_123456789")
    
    MODE = os.getenv("TRADING_MODE", "PAPER").strip().upper()
    if MODE not in ("PAPER", "TESTNET", "LIVE"):
        raise RuntimeError("TRADING_MODE must be PAPER, TESTNET, or LIVE.")

    SYMBOL = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
    PORT = ienv("PORT", 10000, 1)
    DB_FILE = os.getenv("DB_FILE", resolve_db_path("institutional_vault.db")).strip()

    HTTP_TIMEOUT = fenv("REQUEST_TIMEOUT_SEC", 7, 1)
    SCAN_SECONDS = fenv("SCAN_INTERVAL_SEC", 15, 5)
    LEASE_TTL = fenv("LEADER_TTL_SEC", 20, 5)
    MAX_DATA_AGE = fenv("MAX_DATA_AGE_SEC", 90, 10)

    # --- SMALL CAPITAL DELTA EXCHANGE INDIA SETTINGS (Rs 500 - Rs 1000 = ~$10 - $12 USD) ---
    USD_INR_RATE = fenv("USD_INR_RATE", 86.0, 50.0)
    START_EQUITY = fenv("PAPER_EQUITY_USD", 12.0, 5.0)          # ~$12 USD (~Rs 1,000 capital)
    RISK_PCT = fenv("RISK_PER_TRADE_PCT", 0.08, 0.005)          # ~8% of $12 = ~$0.96 (~Rs 80 risk per trade)
    MIN_RISK_USD = fenv("MIN_RISK_USD", 0.50, 0.10)             # Min risk $0.50 (~Rs 43)
    MAX_RISK_USD = fenv("MAX_RISK_USD", 1.50, 0.25)             # Hard cap $1.50 (~Rs 130) loss per trade
    DELTA_LOT_SIZE_BTC = fenv("DELTA_LOT_SIZE_BTC", 0.001, 0.0001) # 1 Lot on Delta = 0.001 BTC
    MAX_LOTS_SMALL_CAP = ienv("MAX_LOTS_SMALL_CAP", 3, 1)       # Max 1 to 3 lots on Rs 500-1000 account
    LEVERAGE = ienv("DEFAULT_LEVERAGE", 25, 1)                  # 25x leverage on Delta

    MAX_DAILY_LOSS = fenv("MAX_DAILY_LOSS_USD", 3.0, 0.5)       # Stop for day if -$3.00 (~Rs 250) lost
    MAX_DAILY_TRADES = ienv("MAX_DAILY_TRADES", 4, 1)           # Max 4 high-conviction trades per day
    MAX_OPEN_TRADES = ienv("MAX_OPEN_TRADES", 1, 1)
    MAX_NOTIONAL = fenv("MAX_NOTIONAL_USD", 300.0, 10.0)

    # --- ANTI-SCALPING & COOLDOWN CONTROLS ---
    SL_COOLDOWN_SECONDS = ienv("SL_COOLDOWN_SECONDS", 2700, 300)     # 45 mins (3 candles of 15m) wait after SL hit
    TRADE_COOLDOWN_SECONDS = ienv("TRADE_COOLDOWN_SECONDS", 900, 60) # Min 15 mins wait after any exit

    FEE_RATE = fenv("PAPER_FEE_RATE", 0.0005, 0)
    SLIPPAGE = fenv("PAPER_SLIPPAGE_RATE", 0.0002, 0)
    MIN_SCORE = fenv("MIN_SIGNAL_SCORE", 80, 0)

    # --- STRUCTURAL STOP LOSS & TARGET BOUNDARIES ---
    MIN_GAP = fenv("MIN_FVG_GAP_USD", 35, 0)
    SWEEP_BUFFER = fenv("SWEEP_BUFFER_USD", 15, 0)
    SL_BUFFER = fenv("STRUCTURAL_SL_BUFFER_USD", 35, 5)
    MIN_STOP = fenv("MIN_STOP_DISTANCE_USD", 140, 50)
    MAX_STOP = fenv("MAX_STOP_DISTANCE_USD", 450, 100)

    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    DELTA_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
    DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
    DELTA_TESTNET = os.getenv("DELTA_TESTNET", "true").strip().lower() == "true"
    DELTA_PRODUCT_ID = int(os.getenv("DELTA_PRODUCT_ID", "27"))

    ALLOWED_SESSIONS = {
        x.strip().upper() for x in os.getenv("ALLOWED_SESSIONS", "LONDON EXPANSION,NEW YORK TREND,ASIAN RANGE").split(",") if x.strip()
    }
except Exception as e:
    sys.exit(f"Startup Config Error: {e}")
