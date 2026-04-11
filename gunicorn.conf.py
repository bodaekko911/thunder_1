bind = "0.0.0.0:8000"
workers = 2
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"
timeout = 120
graceful_timeout = 30
