from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.core.config import BASE_DIR, settings
from app.core.log import logger
from app.db.session import engine


_RUNTIME_SCHEMA_PATCHES: tuple[dict[str, str], ...] = (
    {
        "table": "customers",
        "column": "discount_pct",
        "definition": "NUMERIC(6, 2) DEFAULT 0",
        "backfill": "UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL",
    },
    {
        "table": "invoices",
        "column": "import_batch_id",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "b2b_invoices",
        "column": "import_batch_id",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "consignments",
        "column": "import_batch_id",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "products",
        "column": "created_by_import_batch",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "customers",
        "column": "created_by_import_batch",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
)
_CRITICAL_AUTH_TABLES = {"users", "refresh_tokens", "activity_logs"}


def _alembic_config() -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def _masked_database_url() -> str:
    url = settings.DATABASE_URL
    if "://" not in url or "@" not in url:
        return url
    prefix, rest = url.split("://", 1)
    credentials, host_part = rest.split("@", 1)
    if ":" not in credentials:
        return f"{prefix}://***@{host_part}"
    username, _password = credentials.split(":", 1)
    return f"{prefix}://{username}:***@{host_part}"


def _format_migration_status(payload: dict[str, Any]) -> str:
    if payload["status"] == "ok":
        return "Database migrations are up to date"
    if payload["status"] == "missing_versions":
        return "No Alembic revisions were found"
    if payload["status"] == "legacy_schema_unversioned":
        return "Database schema exists but is not tracked by Alembic"
    if payload["status"] == "pending":
        return "Database migrations are pending"
    if payload["status"] == "schema_incomplete":
        return "Database schema is missing required tables for the current Alembic revision"
    if payload["status"] == "multiple_heads":
        return "Multiple Alembic heads detected"
    return "Database migration status could not be determined"


async def check_migration_status() -> dict[str, Any]:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = sorted(script.get_heads())
    if not heads:
        return {
            "status": "missing_versions",
            "heads": [],
            "current_revisions": [],
            "database_url": _masked_database_url(),
        }
    if len(heads) > 1:
        return {
            "status": "multiple_heads",
            "heads": heads,
            "current_revisions": [],
            "database_url": _masked_database_url(),
        }

    async with engine.begin() as conn:
        def inspect_db(sync_conn):
            db_inspector = inspect(sync_conn)
            table_names = set(db_inspector.get_table_names())
            context = MigrationContext.configure(sync_conn)
            current_revisions = sorted(context.get_current_heads())
            raw_version_rows: list[str] = []
            if "alembic_version" in table_names:
                rows = sync_conn.execute(text("SELECT version_num FROM alembic_version"))
                raw_version_rows.extend(sorted(row[0] for row in rows))
            user_tables = sorted(name for name in table_names if name != "alembic_version")
            return current_revisions, raw_version_rows, user_tables

        current_revisions, raw_version_rows, user_tables = await conn.run_sync(inspect_db)

    if not current_revisions:
        if user_tables:
            return {
                "status": "legacy_schema_unversioned",
                "heads": heads,
                "current_revisions": [],
                "raw_version_rows": raw_version_rows,
                "table_count": len(user_tables),
                "database_url": _masked_database_url(),
            }
        return {
            "status": "pending",
            "heads": heads,
            "current_revisions": [],
            "raw_version_rows": raw_version_rows,
            "database_url": _masked_database_url(),
        }
    if current_revisions != heads:
        return {
            "status": "pending",
            "heads": heads,
            "current_revisions": current_revisions,
            "raw_version_rows": raw_version_rows,
            "database_url": _masked_database_url(),
        }
    missing_tables = sorted(_CRITICAL_AUTH_TABLES - set(user_tables))
    if missing_tables:
        return {
            "status": "schema_incomplete",
            "heads": heads,
            "current_revisions": current_revisions,
            "raw_version_rows": raw_version_rows,
            "missing_tables": missing_tables,
            "database_url": _masked_database_url(),
        }
    return {
        "status": "ok",
        "heads": heads,
        "current_revisions": current_revisions,
        "raw_version_rows": raw_version_rows,
        "database_url": _masked_database_url(),
    }


async def ensure_runtime_schema_compatibility() -> None:
    async with engine.begin() as conn:
        def patch_schema(sync_conn):
            db_inspector = inspect(sync_conn)
            applied: list[str] = []
            for patch in _RUNTIME_SCHEMA_PATCHES:
                table_name = patch["table"]
                if not db_inspector.has_table(table_name):
                    continue
                column_names = {column["name"] for column in db_inspector.get_columns(table_name)}
                if patch["column"] not in column_names:
                    sync_conn.execute(
                        text(
                            f"ALTER TABLE {table_name} ADD COLUMN "
                            f"{patch['column']} {patch['definition']}"
                        )
                    )
                    applied.append(f"{table_name}.{patch['column']}")
                sync_conn.execute(text(patch["backfill"]))
            return applied

        applied = await conn.run_sync(patch_schema)

    if applied:
        logger.warning(
            "Applied runtime schema compatibility patches",
            extra={"schema_patches": applied},
        )


async def verify_migration_status() -> None:
    await ensure_runtime_schema_compatibility()

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
