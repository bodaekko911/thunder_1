from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.core.config import BASE_DIR, settings
from app.core.log import logger
from app.db.session import engine


def _alembic_config() -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def _format_migration_status(payload: dict[str, Any]) -> str:
    if payload["status"] == "ok":
        return "Database migrations are up to date"
    if payload["status"] == "missing_versions":
        return "No Alembic revisions were found"
    if payload["status"] == "legacy_schema_unversioned":
        return "Database schema exists but is not tracked by Alembic"
    if payload["status"] == "pending":
        return "Database migrations are pending"
    if payload["status"] == "multiple_heads":
        return "Multiple Alembic heads detected"
    return "Database migration status could not be determined"


async def check_migration_status() -> dict[str, Any]:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    if not heads:
        return {
            "status": "missing_versions",
            "heads": [],
            "current_revisions": [],
        }
    if len(heads) > 1:
        return {
            "status": "multiple_heads",
            "heads": list(heads),
            "current_revisions": [],
        }

    async with engine.begin() as conn:
        def inspect_db(sync_conn):
            db_inspector = inspect(sync_conn)
            table_names = set(db_inspector.get_table_names())
            current_revisions: list[str] = []
            if "alembic_version" in table_names:
                rows = sync_conn.execute(text("SELECT version_num FROM alembic_version"))
                current_revisions.extend(row[0] for row in rows)
            user_tables = sorted(name for name in table_names if name != "alembic_version")
            return current_revisions, user_tables

        current_revisions, user_tables = await conn.run_sync(inspect_db)

    head = heads[0]
    if not current_revisions:
        if user_tables:
            return {
                "status": "legacy_schema_unversioned",
                "heads": [head],
                "current_revisions": [],
                "table_count": len(user_tables),
            }
        return {
            "status": "pending",
            "heads": [head],
            "current_revisions": [],
        }
    if set(current_revisions) != {head}:
        return {
            "status": "pending",
            "heads": [head],
            "current_revisions": current_revisions,
        }
    return {
        "status": "ok",
        "heads": [head],
        "current_revisions": current_revisions,
    }


async def verify_migration_status() -> None:
    if not settings.MIGRATION_CHECK_ON_STARTUP:
        return

    status = await check_migration_status()
    message = _format_migration_status(status)
    log_extra = {"migration_status": status}

    if status["status"] == "ok":
        logger.info(message, extra=log_extra)
        return

    if settings.MIGRATION_CHECK_STRICT:
        logger.error(message, extra=log_extra)
        raise RuntimeError(message)

    logger.warning(message, extra=log_extra)
