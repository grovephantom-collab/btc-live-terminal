import os, sys, math, sqlite3

def required(name: str, n: int = 24, fallback: str = "") -> str:
    v = os.getenv(name, fallback).strip()
    if len(v) < n:
        raise RuntimeError(f"{name} must be at least {n} chars")
    return v

def fenv(name: str, default: float, minimum: float = 0.0) -> float:
    v = float(os.getenv(name, str(default)))
    if not math.isfinite(v) or v < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
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

    DEFAULT_40_SYMBOLS = (
        "BTCUSDT,ETHUSDT,SOLUSDT,PAXGUSDT,XRPUSDT,DOGEUSDT,SUIUSDT,LINKUSDT,"
        "ADAUSDT,AVAXUSDT,NEARUSDT,DOTUSDT,LTCUSDT,TRXUSDT,APTUSDT,ARBUSDT,"
        "OPUSDT,INJUSDT,TONUSDT,TIAUSDT,SEIUSDT,FETUSDT,RENDERUSDT,WIFUSDT,"
        "PEPEUSDT,SHIBUSDT,BONKUSDT,FLOKIUSDT,ONDOUSDT,ENAUSDT,JUPUSDT,STXUSDT,"
        "FILUSDT,ATOMUSDT,HBARUSDT,ICPUSDT,UNIUSDT,AAVEUSDT,PENDLEUSDT,BCHUSDT"
    )
    SYMBOLS = [s.strip().upper() for s in DEFAULT_40_SYMBOLS.split(",") if s.strip()]
    SYMBOL = SYMBOLS[0]

    ENABLE_ALL_DELTA_RADAR = os.getenv("ENABLE_ALL_DELTA_RADAR", "true").strip().lower() == "true"
    MIN_RADAR_TURNOVER_USD = fenv("MIN_RADAR_TURNOVER_USD", 1500000.0, 100000.0)
    MIN_BINANCE_VOL_USD = fenv("MIN_BINANCE_VOL_USD", 25000000.0, 5000000.0)
    MAX_RADAR_SYMBOLS = ienv("MAX_RADAR_SYMBOLS", 45, 16)
    DISPLAY_SCANNER_CARDS = ienv("DISPLAY_SCANNER_CARDS", 8, 4)
    ENABLE_LIVE_IMPULSE = os.getenv("ENABLE_LIVE_IMPULSE", "true").strip().lower() == "true"

    PORT = ienv("PORT", 10000, 1)
    DB_FILE = os.getenv("DB_FILE", resolve_db_path("institutional_vault.db")).strip()
    HTTP_TIMEOUT = fenv("REQUEST_TIMEOUT_SEC", 7, 1)
    SCAN_SECONDS = fenv("SCAN_INTERVAL_SEC", 15, 5)
    LEASE_TTL = fenv("LEADER_TTL_SEC", 20, 5)
    MAX_DATA_AGE = fenv("MAX_DATA_AGE_SEC", 90, 10)

    USD_INR_RATE = fenv("USD_INR_RATE", 86.0, 50.0)
    START_EQUITY = fenv("PAPER_EQUITY_USD", 58.0, 5.0)
    RISK_PCT = fenv("RISK_PER_TRADE_PCT", 0.032, 0.005)
    A_PLUS_RISK_PCT = fenv("A_PLUS_RISK_PCT", 0.040, 0.005)
    MIN_RISK_USD = fenv("MIN_RISK_USD", 1.50, 0.25)
    MAX_RISK_USD = fenv("MAX_RISK_USD", 2.40, 0.50)
    MAX_LOTS_CAP = ienv("MAX_LOTS_CAP", 600, 1)
    LEVERAGE = ienv("DEFAULT_LEVERAGE", 25, 1)

    DAILY_PROFIT_TARGET = fenv("DAILY_PROFIT_TARGET_USD", 10.00, 2.0)
    MAX_DAILY_LOSS = fenv("MAX_DAILY_LOSS_USD", 7.50, 1.0)
    MAX_DAILY_TRADES = ienv("MAX_DAILY_TRADES", 20, 1)
    MAX_OPEN_TRADES = ienv("MAX_OPEN_TRADES", 3, 1)
    MAX_NOTIONAL = fenv("MAX_NOTIONAL_USD", 850.0, 10.0)

    SL_COOLDOWN_SECONDS = ienv("SL_COOLDOWN_SECONDS", 5400, 300)
    CONSECUTIVE_SL_PAUSE_SEC = ienv("CONSECUTIVE_SL_PAUSE_SEC", 7200, 600)
    TRADE_COOLDOWN_SECONDS = ienv("TRADE_COOLDOWN_SECONDS", 1200, 60)

    STRICT_KILLZONE_ONLY = os.getenv("STRICT_KILLZONE_ONLY", "false").strip().lower() == "true"
    FILTER_DEAD_HOURS = os.getenv("FILTER_DEAD_HOURS", "true").strip().lower() == "true"

    FEE_RATE = fenv("PAPER_FEE_RATE", 0.00059, 0)
    CYCLE_DAYS = ienv("CYCLE_DAYS", 15, 1)
    WITHDRAWAL_RESERVE_PCT = fenv("WITHDRAWAL_RESERVE_PCT", 0.70, 0.0)
    COMPOUND_REINVEST_PCT = fenv("COMPOUND_REINVEST_PCT", 0.30, 0.1)
    SLIPPAGE = fenv("PAPER_SLIPPAGE_RATE", 0.0002, 0)
    MIN_SCORE = fenv("MIN_SIGNAL_SCORE", 94, 0)

    SWING_LOOKBACK_BARS = ienv("SWING_LOOKBACK_BARS", 96, 20)
    STRICT_TREND_LOCK = os.getenv("STRICT_TREND_LOCK", "true").strip().lower() == "true"
    ENABLE_DAILY_BREAKOUT = os.getenv("ENABLE_DAILY_BREAKOUT", "true").strip().lower() == "true"
    ENABLE_CHOCH_STRATEGY = os.getenv("ENABLE_CHOCH_STRATEGY", "true").strip().lower() == "true"
    ENABLE_PREMIUM_DISCOUNT_FILTER = os.getenv("ENABLE_PREMIUM_DISCOUNT_FILTER", "true").strip().lower() == "true"
    ENABLE_CVD_FILTER = os.getenv("ENABLE_CVD_FILTER", "true").strip().lower() == "true"
    ENABLE_DYNAMIC_PROFIT_LOCK = os.getenv("ENABLE_DYNAMIC_PROFIT_LOCK", "true").strip().lower() == "true"
    MIN_HEADROOM_RATIO = fenv("MIN_HEADROOM_RATIO", 1.35, 1.0)
    MIN_ADX_TREND = fenv("MIN_ADX_TREND", 22.0, 10.0)
    MAX_CHOP_BANDWIDTH = fenv("MAX_CHOP_BANDWIDTH", 0.018, 0.005)
    MAX_FUNDING_FOR_LONG = fenv("MAX_FUNDING_FOR_LONG", 0.00035, 0.0)
    MIN_FUNDING_FOR_SHORT = fenv("MIN_FUNDING_FOR_SHORT", -0.00020, -1.0)

    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    DELTA_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
    DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
    DELTA_TESTNET = os.getenv("DELTA_TESTNET", "false" if MODE == "LIVE" else "true").strip().lower() == "true"
    DELTA_PRODUCT_ID = int(os.getenv("DELTA_PRODUCT_ID", "27"))
except Exception as e:
    sys.exit(f"Startup Config Error: {e}")

CATALOG = {
    "BTC":  {"name": "Bitcoin (BTC)",   "dec": 2, "min_s": 400.0,  "max_s": 700.0,  "buf": 55.0,   "gap": 35.0,   "swp": 20.0,   "be": 85.0,   "lot": 0.001, "def_p": 65000.0, "atr": 220.0, "gold": False},
    "PAXG": {"name": "Gold (PAXG/XAU)", "dec": 2, "min_s": 18.0,   "max_s": 42.0,   "buf": 3.0,    "gap": 2.5,    "swp": 1.5,    "be": 5.50,   "lot": 0.01,  "def_p": 3650.0,  "atr": 14.0,  "gold": True},
    "XAU":  {"name": "Gold (PAXG/XAU)", "dec": 2, "min_s": 18.0,   "max_s": 42.0,   "buf": 3.0,    "gap": 2.5,    "swp": 1.5,    "be": 5.50,   "lot": 0.01,  "def_p": 3650.0,  "atr": 14.0,  "gold": True},
    "ETH":  {"name": "Ethereum (ETH)",  "dec": 2, "min_s": 15.0,   "max_s": 32.0,   "buf": 2.2,    "gap": 1.8,    "swp": 1.2,    "be": 2.60,   "lot": 0.01,  "def_p": 2000.0,  "atr": 12.0,  "gold": False},
    "SOL":  {"name": "Solana (SOL)",    "dec": 3, "min_s": 0.95,   "max_s": 2.40,   "buf": 0.16,   "gap": 0.12,   "swp": 0.08,   "be": 0.22,   "lot": 0.1,   "def_p": 75.0,    "atr": 0.85,  "gold": False},
    "XRP":  {"name": "Ripple (XRP)",    "dec": 4, "min_s": 0.012,  "max_s": 0.032,  "buf": 0.0020, "gap": 0.0015, "swp": 0.0010, "be": 0.0025, "lot": 10.0,  "def_p": 0.95,    "atr": 0.012, "gold": False},
    "DOGE": {"name": "Dogecoin (DOGE)", "dec": 5, "min_s": 0.0009, "max_s": 0.0025, "buf": 0.00018,"gap": 0.0001, "swp": 0.0001, "be": 0.0002, "lot": 100.0, "def_p": 0.060,   "atr": 0.0008,"gold": False},
    "SUI":  {"name": "Sui (SUI)",       "dec": 4, "min_s": 0.010,  "max_s": 0.028,  "buf": 0.0020, "gap": 0.0015, "swp": 0.0010, "be": 0.0020, "lot": 10.0,  "def_p": 0.75,    "atr": 0.010, "gold": False},
    "LINK": {"name": "Chainlink (LINK)","dec": 3, "min_s": 0.14,   "max_s": 0.38,   "buf": 0.028,  "gap": 0.025,  "swp": 0.015,  "be": 0.030,  "lot": 1.0,   "def_p": 9.30,    "atr": 0.14,  "gold": False},
    "ADA":  {"name": "Cardano (ADA)",   "dec": 4, "min_s": 0.007,  "max_s": 0.018,  "buf": 0.0012, "gap": 0.0008, "swp": 0.0005, "be": 0.0012, "lot": 10.0,  "def_p": 0.45,    "atr": 0.006, "gold": False},
    "AVAX": {"name": "Avalanche (AVAX)","dec": 3, "min_s": 0.12,   "max_s": 0.32,   "buf": 0.020,  "gap": 0.015,  "swp": 0.010,  "be": 0.020,  "lot": 1.0,   "def_p": 8.0,     "atr": 0.11,  "gold": False},
    "NEAR": {"name": "NEAR (NEAR)",     "dec": 4, "min_s": 0.048,  "max_s": 0.125,  "buf": 0.008,  "gap": 0.006,  "swp": 0.004,  "be": 0.008,  "lot": 1.0,   "def_p": 3.20,    "atr": 0.042, "gold": False},
    "DOT":  {"name": "Polkadot (DOT)",  "dec": 3, "min_s": 0.075,  "max_s": 0.195,  "buf": 0.014,  "gap": 0.010,  "swp": 0.006,  "be": 0.012,  "lot": 1.0,   "def_p": 5.20,    "atr": 0.070, "gold": False},
    "LTC":  {"name": "Litecoin (LTC)",  "dec": 2, "min_s": 1.05,   "max_s": 2.70,   "buf": 0.20,   "gap": 0.14,   "swp": 0.10,   "be": 0.20,   "lot": 0.1,   "def_p": 78.0,    "atr": 1.00,  "gold": False},
    "TRX":  {"name": "TRON (TRX)",      "dec": 5, "min_s": 0.0022, "max_s": 0.0055, "buf": 0.0004, "gap": 0.0003, "swp": 0.0002, "be": 0.0004, "lot": 100.0, "def_p": 0.16,    "atr": 0.0020,"gold": False},
    "APT":  {"name": "Aptos (APT)",     "dec": 3, "min_s": 0.090,  "max_s": 0.250,  "buf": 0.016,  "gap": 0.012,  "swp": 0.008,  "be": 0.015,  "lot": 1.0,   "def_p": 6.50,    "atr": 0.085, "gold": False},
    "ARB":  {"name": "Arbitrum (ARB)",  "dec": 4, "min_s": 0.0026, "max_s": 0.0078, "buf": 0.0006, "gap": 0.0004, "swp": 0.0003, "be": 0.0005, "lot": 10.0,  "def_p": 0.14,    "atr": 0.0022,"gold": False},
}

def get_asset_params(symbol: str, current_price: float = None) -> dict:
    sym = symbol.strip().upper()
    base = sym.replace("USDT", "").replace("USD", "")
    if base in CATALOG:
        c = CATALOG[base]
        p = float(current_price or c["def_p"])
        ratio = (p / c["def_p"]) if (p > 0 and c["def_p"] > 0) else 1.0
        scale = ratio if (ratio < 0.5 or ratio > 1.8) else 1.0
        dec = c["dec"]
        return {
            "symbol": sym, "is_gold": c["gold"], "decimals": dec, "display_name": c["name"],
            "cb_symbol": f"{base}-USD", "bin_symbol": f"{base}USDT", "delta_symbol": f"{base}USD",
            "min_stop": round(c["min_s"] * scale, dec), "max_stop": round(c["max_s"] * scale, dec),
            "sl_buffer": round(c["buf"] * scale, dec), "min_gap": round(c["gap"] * scale, dec),
            "sweep_buffer": round(c["swp"] * scale, dec), "be_buffer": round(c["be"] * scale, dec),
            "lot_size": c["lot"], "default_price": p, "default_atr": round(c["atr"] * scale, dec),
        }
    p = float(current_price or 1.0)
    dec = 6 if p < 0.005 else (5 if p < 0.2 else (4 if p < 5.0 else (3 if p < 50.0 else 2)))
    lot = 1000.0 if p < 0.005 else (100.0 if p < 0.2 else (10.0 if p < 2.0 else (1.0 if p < 50.0 else 0.1)))
    return {
        "symbol": sym, "is_gold": False, "decimals": dec, "display_name": f"{base} ({base})",
        "cb_symbol": f"{base}-USD", "bin_symbol": f"{base}USDT", "delta_symbol": f"{base}USD",
        "min_stop": round(p * 0.013, dec), "max_stop": round(p * 0.030, dec),
        "sl_buffer": round(p * 0.0020, dec), "min_gap": round(p * 0.0012, dec),
        "sweep_buffer": round(p * 0.0008, dec), "be_buffer": round(p * 0.0020, dec),
        "lot_size": lot, "default_price": p, "default_atr": round(p * 0.011, dec),
    }
