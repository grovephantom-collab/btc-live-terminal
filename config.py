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

    # --- MULTI-MARKET SCANNER: BTC, ETH, SOL, GOLD (PAXG) ---
    SYMBOLS_STR = os.getenv("SYMBOLS", os.getenv("SYMBOL", "BTCUSDT,ETHUSDT,SOLUSDT,PAXGUSDT")).strip()
    SYMBOLS = [s.strip().upper() for s in SYMBOLS_STR.split(",") if s.strip()]
    if not SYMBOLS:
        SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "PAXGUSDT"]
    SYMBOL = SYMBOLS[0]

    PORT = ienv("PORT", 10000, 1)
    DB_FILE = os.getenv("DB_FILE", resolve_db_path("institutional_vault.db")).strip()

    HTTP_TIMEOUT = fenv("REQUEST_TIMEOUT_SEC", 7, 1)
    SCAN_SECONDS = fenv("SCAN_INTERVAL_SEC", 15, 5)
    LEASE_TTL = fenv("LEADER_TTL_SEC", 20, 5)
    MAX_DATA_AGE = fenv("MAX_DATA_AGE_SEC", 90, 10)

    # --- Rs 5,000 (~$58 USD) CAPITAL SIZING & RISK MANAGEMENT ---
    USD_INR_RATE = fenv("USD_INR_RATE", 86.0, 50.0)
    START_EQUITY = fenv("PAPER_EQUITY_USD", 58.0, 5.0)
    RISK_PCT = fenv("RISK_PER_TRADE_PCT", 0.025, 0.005)
    MIN_RISK_USD = fenv("MIN_RISK_USD", 0.50, 0.10)
    MAX_RISK_USD = fenv("MAX_RISK_USD", 2.20, 0.20)
    MAX_LOTS_CAP = ienv("MAX_LOTS_CAP", 10, 1)
    LEVERAGE = ienv("DEFAULT_LEVERAGE", 25, 1)

    MAX_DAILY_LOSS = fenv("MAX_DAILY_LOSS_USD", 5.00, 0.5)
    MAX_DAILY_TRADES = ienv("MAX_DAILY_TRADES", 6, 1)
    MAX_OPEN_TRADES = ienv("MAX_OPEN_TRADES", 2, 1)
    MAX_NOTIONAL = fenv("MAX_NOTIONAL_USD", 600.0, 10.0)

    # --- ANTI-WHIPSAW & CIRCUIT BREAKER RULES ---
    SL_COOLDOWN_SECONDS = ienv("SL_COOLDOWN_SECONDS", 3600, 300)
    CONSECUTIVE_SL_PAUSE_SEC = ienv("CONSECUTIVE_SL_PAUSE_SEC", 9000, 600)
    TRADE_COOLDOWN_SECONDS = ienv("TRADE_COOLDOWN_SECONDS", 1200, 60)

    STRICT_KILLZONE_ONLY = os.getenv("STRICT_KILLZONE_ONLY", "false").strip().lower() == "true"
    FILTER_DEAD_HOURS = os.getenv("FILTER_DEAD_HOURS", "true").strip().lower() == "true"

    FEE_RATE = fenv("PAPER_FEE_RATE", 0.0005, 0)
    SLIPPAGE = fenv("PAPER_SLIPPAGE_RATE", 0.0002, 0)
    MIN_SCORE = fenv("MIN_SIGNAL_SCORE", 88, 0)

    # --- 1-DAY STRUCTURAL LOOKBACK & INSTITUTIONAL FILTERS ---
    SWING_LOOKBACK_BARS = ienv("SWING_LOOKBACK_BARS", 96, 20)
    STRICT_TREND_LOCK = os.getenv("STRICT_TREND_LOCK", "true").strip().lower() == "true"
    ENABLE_DAILY_BREAKOUT = os.getenv("ENABLE_DAILY_BREAKOUT", "true").strip().lower() == "true"
    ENABLE_CHOCH_STRATEGY = os.getenv("ENABLE_CHOCH_STRATEGY", "true").strip().lower() == "true"
    ENABLE_PREMIUM_DISCOUNT_FILTER = os.getenv("ENABLE_PREMIUM_DISCOUNT_FILTER", "true").strip().lower() == "true"
    ENABLE_CVD_FILTER = os.getenv("ENABLE_CVD_FILTER", "true").strip().lower() == "true"
    ENABLE_DYNAMIC_PROFIT_LOCK = os.getenv("ENABLE_DYNAMIC_PROFIT_LOCK", "true").strip().lower() == "true"
    MIN_HEADROOM_RATIO = fenv("MIN_HEADROOM_RATIO", 1.25, 1.0)
    MIN_ADX_TREND = fenv("MIN_ADX_TREND", 22.0, 10.0)
    MAX_CHOP_BANDWIDTH = fenv("MAX_CHOP_BANDWIDTH", 0.018, 0.005)

    MAX_FUNDING_FOR_LONG = fenv("MAX_FUNDING_FOR_LONG", 0.00035, 0.0)
    MIN_FUNDING_FOR_SHORT = fenv("MIN_FUNDING_FOR_SHORT", -0.00020, -1.0)

    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    DELTA_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
    DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
    _default_testnet = "false" if MODE == "LIVE" else "true"
    DELTA_TESTNET = os.getenv("DELTA_TESTNET", _default_testnet).strip().lower() == "true"
    DELTA_PRODUCT_ID = int(os.getenv("DELTA_PRODUCT_ID", "27"))

except Exception as e:
    sys.exit(f"Startup Config Error: {e}")

def get_asset_params(symbol: str) -> dict:
    """Returns asset-specific volatility, sizing, and Delta Exchange contract specs."""
    sym = symbol.strip().upper()
    if ("XAU" in sym) or ("GOLD" in sym) or ("PAXG" in sym):
        return {
            "symbol": sym,
            "is_gold": True,
            "display_name": "Gold (PAXG/XAU)",
            "cb_symbol": "PAXG-USD",
            "bin_symbol": "PAXGUSDT",
            "delta_symbol": "PAXGUSD",
            "min_stop": float(os.getenv("GOLD_MIN_STOP", "3.8")),
            "max_stop": float(os.getenv("GOLD_MAX_STOP", "6.8")),
            "sl_buffer": float(os.getenv("GOLD_SL_BUFFER", "0.6")),
            "min_gap": float(os.getenv("GOLD_MIN_GAP", "0.8")),
            "sweep_buffer": float(os.getenv("GOLD_SWEEP_BUFFER", "0.4")),
            "lot_size": float(os.getenv("GOLD_LOT_SIZE", "0.01")),
            "default_price": 2700.0,
            "default_atr": 4.5
        }
    if "ETH" in sym:
        return {
            "symbol": sym,
            "is_gold": False,
            "display_name": "Ethereum (ETH)",
            "cb_symbol": "ETH-USD",
            "bin_symbol": "ETHUSDT",
            "delta_symbol": "ETHUSD",
            "min_stop": float(os.getenv("ETH_MIN_STOP", "16.0")),
            "max_stop": float(os.getenv("ETH_MAX_STOP", "32.0")),
            "sl_buffer": float(os.getenv("ETH_SL_BUFFER", "2.5")),
            "min_gap": float(os.getenv("ETH_MIN_GAP", "2.0")),
            "sweep_buffer": float(os.getenv("ETH_SWEEP_BUFFER", "1.5")),
            "lot_size": float(os.getenv("ETH_LOT_SIZE", "0.01")),
            "default_price": 3000.0,
            "default_atr": 14.0
        }
    if "SOL" in sym:
        return {
            "symbol": sym,
            "is_gold": False,
            "display_name": "Solana (SOL)",
            "cb_symbol": "SOL-USD",
            "bin_symbol": "SOLUSDT",
            "delta_symbol": "SOLUSD",
            "min_stop": float(os.getenv("SOL_MIN_STOP", "1.2")),
            "max_stop": float(os.getenv("SOL_MAX_STOP", "2.8")),
            "sl_buffer": float(os.getenv("SOL_SL_BUFFER", "0.2")),
            "min_gap": float(os.getenv("SOL_MIN_GAP", "0.15")),
            "sweep_buffer": float(os.getenv("SOL_SWEEP_BUFFER", "0.1")),
            "lot_size": float(os.getenv("SOL_LOT_SIZE", "0.1")),
            "default_price": 180.0,
            "default_atr": 1.1
        }
    return {
        "symbol": sym,
        "is_gold": False,
        "display_name": "Bitcoin (BTC)",
        "cb_symbol": "BTC-USD",
        "bin_symbol": "BTCUSDT",
        "delta_symbol": "BTCUSD",
        "min_stop": float(os.getenv("MIN_STOP_DISTANCE_USD", "380")),
        "max_stop": float(os.getenv("MAX_STOP_DISTANCE_USD", "620")),
        "sl_buffer": float(os.getenv("STRUCTURAL_SL_BUFFER_USD", "50")),
        "min_gap": float(os.getenv("MIN_FVG_GAP_USD", "35")),
        "sweep_buffer": float(os.getenv("SWEEP_BUFFER_USD", "20")),
        "lot_size": float(os.getenv("DELTA_LOT_SIZE_BTC", "0.001")),
        "default_price": 75000.0,
        "default_atr": 220.0
    }
