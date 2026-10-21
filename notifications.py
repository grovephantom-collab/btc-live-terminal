import queue, time, logging, requests
import config

log = logging.getLogger("TelegramEngine")
TG_QUEUE = queue.Queue(maxsize=100)
HTTP = requests.Session()

def enqueue_telegram(msg: str):
    if config.TG_TOKEN and config.TG_CHAT:
        try: TG_QUEUE.put_nowait(msg[:3500])
        except queue.Full: log.warning("Telegram queue saturated; alert dropped")

def telegram_worker_loop():
    while True:
        msg = TG_QUEUE.get()
        try:
            if config.TG_TOKEN and config.TG_CHAT:
                for attempt in range(4):
                    try:
                        r = HTTP.post(f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage",
                                    json={"chat_id": config.TG_CHAT, "text": msg, "disable_web_page_preview": True}, timeout=8)
                        if r.status_code == 200: break
                        if r.status_code == 429:
                            w = float(r.json().get("parameters", {}).get("retry_after", 3))
                            time.sleep(min(30, max(1, w)))
                        else: time.sleep(1.5 * (attempt + 1))
                    except Exception: time.sleep(1.5 * (attempt + 1))
        finally: TG_QUEUE.task_done()
