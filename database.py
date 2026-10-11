import os, time, uuid, sqlite3, json
from datetime import datetime, timezone
from typing import Tuple, Optional
import config

INSTANCE = str(uuid.uuid4())[:8]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def jd(x) -> str:
    return json.dumps(x, separators=(",", ":"), sort_keys=True, default=str)

def db() -> sqlite3.Connection:
    if os.path.dirname(config.DB_FILE):
        os.makedirs(os.path.dirname(os.path.abspath(config.DB_FILE)), exist_ok=True)
    c = sqlite3.connect(config.DB_FILE, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    # 30. SQLite WAL & Foreign Key Integrity
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=20000")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c = db()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leader_lease(
            id INTEGER PRIMARY KEY CHECK(id=1),
            leader_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            acquired_at_utc TEXT NOT NULL,
            expires_at_epoch REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT UNIQUE NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_name TEXT NOT NULL,
            score REAL NOT NULL,
            entry_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            tp1_price REAL NOT NULL,
            tp2_price REAL NOT NULL,
            tp3_price REAL NOT NULL,
            timeframe TEXT NOT NULL,
            candle_time INTEGER NOT NULL,
            details_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT UNIQUE NOT NULL,
            signal_key TEXT UNIQUE NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_name TEXT NOT NULL,
            score REAL NOT NULL,
            entry_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            tp1_price REAL NOT NULL,
            tp2_price REAL NOT NULL,
            tp3_price REAL NOT NULL,
            initial_qty REAL NOT NULL,
            remaining_qty REAL NOT NULL,
            tp1_done INTEGER NOT NULL DEFAULT 0,
            tp2_done INTEGER NOT NULL DEFAULT 0,
            tp3_done INTEGER NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT 'OPEN',
            realized_pnl_usd REAL NOT NULL DEFAULT 0,
            unrealized_pnl_usd REAL NOT NULL DEFAULT 0,
            total_fee_usd REAL NOT NULL DEFAULT 0,
            weighted_pnl_r REAL NOT NULL DEFAULT 0,
            close_reason TEXT,
            opened_at_utc TEXT NOT NULL,
            closed_at_utc TEXT,
            updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trade_audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL,
            FOREIGN KEY(trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_trades_stage ON trades(stage);
        """)
    finally:
        c.close()

def audit(c: sqlite3.Connection, tid: str, event: str, details: dict):
    c.execute("INSERT INTO trade_audit_log (trade_id,event_type,details_json,timestamp_utc) VALUES(?,?,?,?)",
              (tid, event, jd(details), now_iso()))

# 29. Single-Leader Lease Lock
def lease_tick() -> Tuple[bool, Optional[int]]:
    t = time.time()
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM leader_lease WHERE id=1").fetchone()
        if r is None:
            token = 1
            c.execute("INSERT INTO leader_lease VALUES(1,?,?,?,?)", (INSTANCE, token, now_iso(), t + config.LEASE_TTL))
            c.commit()
            return True, token
        if r["leader_id"] == INSTANCE and r["expires_at_epoch"] > t:
            token = int(r["fencing_token"])
            n = c.execute("UPDATE leader_lease SET acquired_at_utc=?,expires_at_epoch=? WHERE id=1 AND leader_id=? AND fencing_token=? AND expires_at_epoch>?",
                          (now_iso(), t + config.LEASE_TTL, INSTANCE, token, t)).rowcount
            c.commit()
            return n == 1, token if n == 1 else None
        if r["expires_at_epoch"] <= t:
            token = int(r["fencing_token"]) + 1
            n = c.execute("UPDATE leader_lease SET leader_id=?,fencing_token=?,acquired_at_utc=?,expires_at_epoch=? WHERE id=1 AND fencing_token=? AND expires_at_epoch<=?",
                          (INSTANCE, token, now_iso(), t + config.LEASE_TTL, int(r["fencing_token"]), t)).rowcount
            c.commit()
            return n == 1, token if n == 1 else None
        c.commit()
        return False, None
    except Exception:
        c.rollback()
        return False, None
    finally:
        c.close()

def leader_ok(token: Optional[int]) -> bool:
    if token is None: return False
    c = db()
    try:
        r = c.execute("SELECT * FROM leader_lease WHERE id=1").fetchone()
        return bool(r and r["leader_id"] == INSTANCE and int(r["fencing_token"]) == int(token) and r["expires_at_epoch"] > time.time())
    finally:
        c.close()
