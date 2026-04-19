from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.config import settings


def app_tz() -> ZoneInfo:
    return ZoneInfo(settings.APP_TIMEZONE)


def today_local() -> date:
    return datetime.now(app_tz()).date()


def now_local() -> datetime:
    return datetime.now(app_tz())


def utc_bounds(local_start: date, local_end: date) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) covering the full local-date range in APP_TIMEZONE."""
    tz  = app_tz()
    utc = ZoneInfo("UTC")
    s = datetime(local_start.year, local_start.month, local_start.day,
                 0, 0, 0, tzinfo=tz).astimezone(utc)
    e = datetime(local_end.year, local_end.month, local_end.day,
                 23, 59, 59, 999999, tzinfo=tz).astimezone(utc)
    return s, e
