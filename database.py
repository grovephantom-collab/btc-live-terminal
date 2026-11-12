import sqlite3, time, uuid, os
from datetime import datetime, timezone
import config

LEASE_ID = str(uuid.uuid4())

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_FILE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        try:
            conn.execute("PRAGMA journal_mode=DELETE;")
        except Exception:
            pass
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    conn = db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leader_lease (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                leader_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                lease_expires_at REAL NOT NULL,
                acquired_at_utc TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                signal_key TEXT UNIQUE,
                symbol TEXT,
                direction TEXT,
                setup_name TEXT,
                score REAL,
                entry_price REAL,
                stop_price REAL,
                tp1_price REAL,
                tp2_price REAL,
                tp3_price REAL,
                initial_qty REAL,
                remaining_qty REAL,
                tp1_done INTEGER DEFAULT 0,
                tp2_done INTEGER DEFAULT 0,
                stage TEXT,
                realized_pnl_usd REAL DEFAULT 0,
                unrealized_pnl_usd REAL DEFAULT 0,
                total_fee_usd REAL DEFAULT 0,
                close_reason TEXT,
                opened_at_utc TEXT,
                closed_at_utc TEXT,
                updated_at_utc TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at_utc TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('auto_trade_enabled', '1')")
        conn.commit()
    finally:
        conn.close()

def get_setting(key: str, default: str = "1") -> str:
    conn = db()
    try:
        row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default
    except Exception:
        return default
    finally:
        conn.close()

def set_setting(key: str, value: str):
    conn = db()
    try:
        conn.execute("INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        conn.commit()
    finally:
        conn.close()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def lease_tick() -> tuple:
    now = time.time()
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT leader_id, fencing_token, lease_expires_at FROM leader_lease WHERE id = 1")
        row = cur.fetchone()
        if not row:
            try:
                cur.execute(
                    "INSERT INTO leader_lease (id, leader_id, fencing_token, lease_expires_at, acquired_at_utc) VALUES (1, ?, 1, ?, ?)",
                    (LEASE_ID, now + config.LEASE_TTL, now_iso())
                )
                conn.commit()
                return True, 1
            except sqlite3.IntegrityError:
                pass

        leader_id = row["leader_id"]
        token = row["fencing_token"]
        expires = row["lease_expires_at"]

        if leader_id == LEASE_ID:
            cur.execute("UPDATE leader_lease SET lease_expires_at = ? WHERE id = 1 AND leader_id = ?", (now + config.LEASE_TTL, LEASE_ID))
            conn.commit()
            return True, token

        if now > expires:
            new_token = token + 1
            cur.execute(
                "UPDATE leader_lease SET leader_id = ?, fencing_token = ?, lease_expires_at = ?, acquired_at_utc = ? WHERE id = 1 AND fencing_token = ?",
                (LEASE_ID, new_token, now + config.LEASE_TTL, now_iso(), token)
            )
            conn.commit()
            if cur.rowcount > 0:
                return True, new_token

        return False, None
    finally:
        conn.close()

def leader_ok(fencing_token: int) -> bool:
    if fencing_token is None:
        return False
    conn = db()
    try:
        row = conn.execute("SELECT leader_id, fencing_token, lease_expires_at FROM leader_lease WHERE id = 1").fetchone()
        if not row:
            return False
        return (row["leader_id"] == LEASE_ID and row["fencing_token"] == fencing_token and time.time() <= row["lease_expires_at"])
    finally:
        conn.close()
