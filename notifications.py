import time, logging, requests
import database, config

log = logging.getLogger("Notifications")

def enqueue_telegram(msg: str):
    conn = database.db()
    try:
        conn.execute("INSERT INTO notifications (message, status, created_at_utc) VALUES (?, 'PENDING', ?)", (msg, database.now_iso()))
        conn.commit()
    finally:
        conn.close()

def telegram_worker_loop():
    while True:
        try:
            conn = database.db()
            row = conn.execute("SELECT id, message FROM notifications WHERE status = 'PENDING' ORDER BY id ASC LIMIT 1").fetchone()
            if not row:
                conn.close()
                time.sleep(3)
                continue

            nid = row["id"]
            msg = row["message"]

            if config.TG_TOKEN and config.TG_CHAT:
                url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage"
                try:
                    requests.post(url, json={"chat_id": config.TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=5)
                except Exception as ex:
                    log.warning("Telegram send failed: %s", ex)

            conn.execute("UPDATE notifications SET status = 'SENT' WHERE id = ?", (nid,))
            conn.commit()
            conn.close()
            time.sleep(0.5)
        except Exception as e:
            log.warning("Notification worker error: %s", e)
            time.sleep(3)
