import os

from app.core.config import settings


# Railway injects PORT at runtime; local runs fall back to API_PORT.
_port = os.environ.get("PORT") or str(settings.API_PORT)
bind = f"{settings.API_HOST}:{_port}"
workers = settings.WORKERS
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"
loglevel = settings.LOG_LEVEL.lower()
timeout = 120
graceful_timeout = 30
