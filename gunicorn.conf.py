from app.core.config import settings


bind = f"{settings.API_HOST}:{settings.API_PORT}"
workers = settings.WORKERS
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"
loglevel = settings.LOG_LEVEL.lower()
timeout = 120
graceful_timeout = 30
