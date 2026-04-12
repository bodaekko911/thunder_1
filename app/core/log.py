import contextvars
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.core.config import settings
from app.database import Base


REQUEST_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "request_log_context",
    default={},
)
STANDARD_LOG_RECORD_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


def bind_log_context(**values: Any) -> contextvars.Token[dict[str, Any]]:
    context = dict(REQUEST_LOG_CONTEXT.get())
    context.update({key: value for key, value in values.items() if value is not None})
    return REQUEST_LOG_CONTEXT.set(context)


def reset_log_context(token: contextvars.Token[dict[str, Any]]) -> None:
    REQUEST_LOG_CONTEXT.reset(token)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    return str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(REQUEST_LOG_CONTEXT.get())
        for key, value in record.__dict__.items():
            if key in STANDARD_LOG_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = _serialize_value(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging() -> None:
    log_path = Path(settings.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.LOG_LEVEL.upper())
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    logging.captureWarnings(True)


logger = logging.getLogger("erp")


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    user_name = Column(String(150))
    user_role = Column(String(50))
    action = Column(String(100))
    module = Column(String(50))
    description = Column(Text)
    ref_type = Column(String(50))
    ref_id = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def record(
    db,
    module: str,
    action: str,
    description: str,
    user=None,
    ref_type: str = None,
    ref_id: str = None,
):
    entry = ActivityLog(
        user_id=user.id if user else None,
        user_name=user.name if user else "System",
        user_role=user.role if user else "system",
        action=action,
        module=module,
        description=description,
        ref_type=ref_type,
        ref_id=str(ref_id) if ref_id is not None else None,
    )
    db.add(entry)
