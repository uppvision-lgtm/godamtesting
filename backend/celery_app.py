import os
import json
import logging
from datetime import timedelta

from celery import Celery
from dotenv import load_dotenv


load_dotenv()

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.getLogger().handlers.clear()
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO"))

celery_app = Celery("instagram_worker", broker=redis_url, backend=redis_url)
celery_app.conf.update(
    include=["tasks", "video_tasks"],
    timezone="Asia/Jakarta",
    enable_utc=False,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=int(os.getenv("CELERY_CONCURRENCY", "1")),
    worker_max_tasks_per_child=10,
    result_expires=timedelta(days=1),
)