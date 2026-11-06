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

    # --- SMALL CAPITAL DELTA EXCHANGE SIZING (Rs 500 - Rs 1000 = ~$6 - $12 USD) ---
    USD_INR_RATE = fenv("USD_INR_RATE", 86.0, 50.0)
    START_EQUITY = fenv("PAPER_EQUITY_USD", 12.0, 5.0)          # ~$12 USD (~Rs 1,000 capital)
    RISK_PCT = fenv("RISK_PER_TRADE_PCT", 0.05, 0.005)          # 5% max risk
    MIN_RISK_USD = fenv("MIN_RISK_USD", 0.35, 0.10)             # Min $0.35 (~Rs 30) risk
    MAX_RISK_USD = fenv("MAX_RISK_USD", 0.85, 0.20)             # Hard cap $0.85 (~Rs 73) loss
    DELTA_LOT_SIZE_BTC = fenv("DELTA_LOT_SIZE_BTC", 0.001, 0.0001) # 1 Lot = 0.001 BTC on Delta
    MAX_LOTS_CAP = ienv("MAX_LOTS_CAP", 3, 1)                   # Max 1-2 lots
    LEVERAGE = ienv("DEFAULT_LEVERAGE", 25, 1)

    MAX_DAILY_LOSS = fenv("MAX_DAILY_LOSS_USD", 2.20, 0.5)      # Daily stop: -$2.20 (~Rs 190)
    MAX_DAILY_TRADES = ienv("MAX_DAILY_TRADES", 3, 1)           # Max 3 high-conviction trades per day
    MAX_OPEN_TRADES = ienv("MAX_OPEN_TRADES", 1, 1)
    MAX_NOTIONAL = fenv("MAX_NOTIONAL_USD", 250.0, 10.0)

    # --- ANTI-WHIPSAW & CIRCUIT BREAKER RULES ---
    SL_COOLDOWN_SECONDS = ienv("SL_COOLDOWN_SECONDS", 3600, 300)        # 60 mins wait after SL hit
    CONSECUTIVE_SL_PAUSE_SEC = ienv("CONSECUTIVE_SL_PAUSE_SEC", 9000, 600) # 2.5 hours pause after 2 SL hits
    TRADE_COOLDOWN_SECONDS = ienv("TRADE_COOLDOWN_SECONDS", 1200, 60)    # 20 mins wait after profit exit
    
    # Session Filter: London & New York
    STRICT_KILLZONE_ONLY = os.getenv("STRICT_KILLZONE_ONLY", "false").strip().lower() == "true"

    FEE_RATE = fenv("PAPER_FEE_RATE", 0.0005, 0)
    SLIPPAGE = fenv("PAPER_SLIPPAGE_RATE", 0.0002, 0)
    MIN_SCORE = fenv("MIN_SIGNAL_SCORE", 88, 0)

    # --- 1-DAY (24-HOUR) STRUCTURAL LOOKBACK & MACRO TREND LOCK ---
    SWING_LOOKBACK_BARS = ienv("SWING_LOOKBACK_BARS", 96, 20)      # 96 bars = 24 Hours of 15M candles (1 Day)
    STRICT_TREND_LOCK = os.getenv("STRICT_TREND_LOCK", "true").strip().lower() == "true" # 100% block counter-trend trades
    ENABLE_DAILY_BREAKOUT = os.getenv("ENABLE_DAILY_BREAKOUT", "true").strip().lower() == "true" # Trade expansion breakouts
    ENABLE_CHOCH_STRATEGY = os.getenv("ENABLE_CHOCH_STRATEGY", "true").strip().lower() == "true" # Trade structural trend shifts
    MIN_HEADROOM_RATIO = fenv("MIN_HEADROOM_RATIO", 1.25, 1.0)     # Minimum distance to nearest major resistance/support

    # --- VOLATILITY-NORMALIZED WIDE STOP LOSS BOUNDARIES ---
    MIN_GAP = fenv("MIN_FVG_GAP_USD", 35, 0)
    SWEEP_BUFFER = fenv("SWEEP_BUFFER_USD", 20, 0)
    SL_BUFFER = fenv("STRUCTURAL_SL_BUFFER_USD", 50, 5)         # 50 pts buffer beyond swing anchor
    MIN_STOP = fenv("MIN_STOP_DISTANCE_USD", 380, 150)          # Hard floor 380 pts (anti-noise)
    MAX_STOP = fenv("MAX_STOP_DISTANCE_USD", 620, 200)          # Hard ceiling 620 pts
    MIN_ADX_TREND = fenv("MIN_ADX_TREND", 22.0, 10.0)           # ADX >= 22 for trend continuation & CHoCH
    MAX_CHOP_BANDWIDTH = fenv("MAX_CHOP_BANDWIDTH", 0.018, 0.005)

    # Funding Rate Filter
    MAX_FUNDING_FOR_LONG = fenv("MAX_FUNDING_FOR_LONG", 0.00035, 0.0)
    MIN_FUNDING_FOR_SHORT = fenv("MIN_FUNDING_FOR_SHORT", -0.00020, -1.0)

    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # Delta Exchange Config
    DELTA_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
    DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
    DELTA_TESTNET = os.getenv("DELTA_TESTNET", "true").strip().lower() == "true"
    DELTA_PRODUCT_ID = int(os.getenv("DELTA_PRODUCT_ID", "27"))

except Exception as e:
    sys.exit(f"Startup Config Error: {e}")
