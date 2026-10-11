import os, sys, math

def required(name: str, n: int = 24) -> str:
    v = os.getenv(name, "").strip()
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

try:
    # 1. P0 Fail-Safe Secrets Check
    API_SECRET_KEY = required("API_SECRET_KEY", 24)
    WORKER_AUTH_TOKEN = required("WORKER_AUTH_TOKEN", 24)
    
    # 2. Paper-Only Air-Gap Isolation
    MODE = os.getenv("TRADING_MODE", "PAPER").strip().upper()
    if MODE != "PAPER":
        raise RuntimeError("Engine is hard-locked to PAPER mode for capital protection.")

    SYMBOL = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
    PORT = ienv("PORT", 10000, 1)
    DB_FILE = os.getenv("DB_FILE", "institutional_vault.db").strip()

    HTTP_TIMEOUT = fenv("REQUEST_TIMEOUT_SEC", 7, 1)
    SCAN_SECONDS = fenv("SCAN_INTERVAL_SEC", 12, 3)
    LEASE_TTL = fenv("LEADER_TTL_SEC", 15, 5)
    MAX_DATA_AGE = fenv("MAX_DATA_AGE_SEC", 90, 10)

    # 4. Account Risk Thresholds
    START_EQUITY = fenv("PAPER_EQUITY_USD", 1000, 10)
    RISK_PCT = fenv("RISK_PER_TRADE_PCT", 0.01, 0.0001)
    MAX_DAILY_LOSS = fenv("MAX_DAILY_LOSS_USD", 50, 1)
    MAX_DAILY_TRADES = ienv("MAX_DAILY_TRADES", 3, 1)
    MAX_OPEN_TRADES = ienv("MAX_OPEN_TRADES", 1, 1)
    MAX_NOTIONAL = fenv("MAX_NOTIONAL_USD", 1000, 10)

    # 5. Execution Fees & Slippage
    FEE_RATE = fenv("PAPER_FEE_RATE", 0.0005, 0)
    SLIPPAGE = fenv("PAPER_SLIPPAGE_RATE", 0.0003, 0)
    MIN_SCORE = fenv("MIN_SIGNAL_SCORE", 75, 0)

    # 3. Structural SMC Thresholds
    MIN_GAP = fenv("MIN_FVG_GAP_USD", 35, 0)
    SWEEP_BUFFER = fenv("SWEEP_BUFFER_USD", 10, 0)
    SL_BUFFER = fenv("STRUCTURAL_SL_BUFFER_USD", 25, 0)
    MIN_STOP = fenv("MIN_STOP_DISTANCE_USD", 40, 1)
    MAX_STOP = fenv("MAX_STOP_DISTANCE_USD", 1500, 1)

    # 6. Notifications
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    ALLOWED_SESSIONS = {
        x.strip().upper() for x in os.getenv("ALLOWED_SESSIONS", "LONDON EXPANSION,NEW YORK TREND").split(",") if x.strip()
    }
except Exception as e:
    sys.exit(f"Startup Config Error: {e}")
