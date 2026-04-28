from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.core.config import BASE_DIR, settings
from app.db.base import Base
from app.db.session import engine


CREATE_TABLE_PATTERN = re.compile(r'op\.create_table\(\s*["\']([^"\']+)["\']')


@dataclass
class SchemaReport:
    database_url_masked: str
    alembic_heads: list[str]
    current_revisions: list[str]
    actual_tables: list[str]
    expected_tables: list[str]
    missing_tables: list[str]
    unexpected_tables: list[str]
    missing_table_migrations: dict[str, list[str]]
    likely_issue: str


def _masked_database_url() -> str:
    url = settings.DATABASE_URL
    if "@" not in url or "://" not in url:
        return url
    prefix, rest = url.split("://", 1)
    credentials, host_part = rest.split("@", 1)
    if ":" not in credentials:
        return f"{prefix}://***@{host_part}"
    username, _password = credentials.split(":", 1)
    return f"{prefix}://{username}:***@{host_part}"


def _alembic_config() -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def _migration_table_map() -> dict[str, list[str]]:
    versions_dir = BASE_DIR / "alembic" / "versions"
    mapping: dict[str, list[str]] = {}
    for path in sorted(versions_dir.glob("*.py")):
        for table_name in CREATE_TABLE_PATTERN.findall(path.read_text(encoding="utf-8")):
            mapping.setdefault(table_name, []).append(path.stem)
    return mapping


async def build_report() -> SchemaReport:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = sorted(script.get_heads())
    expected_tables = sorted(Base.metadata.tables.keys())
    table_map = _migration_table_map()

    async with engine.begin() as conn:
        def inspect_db(sync_conn):
            db_inspector = inspect(sync_conn)
            actual_tables = sorted(db_inspector.get_table_names())
            current_revisions: list[str] = []
            if "alembic_version" in actual_tables:
                rows = sync_conn.execute(text("SELECT version_num FROM alembic_version"))
                current_revisions.extend(sorted(row[0] for row in rows))
            return current_revisions, actual_tables

        current_revisions, actual_tables = await conn.run_sync(inspect_db)

    actual_user_tables = [name for name in actual_tables if name != "alembic_version"]
    missing_tables = sorted(set(expected_tables) - set(actual_user_tables))
    unexpected_tables = sorted(set(actual_user_tables) - set(expected_tables))
    missing_table_migrations = {
        table_name: table_map.get(table_name, [])
        for table_name in missing_tables
    }

    auth_only_tables = {"activity_logs", "refresh_tokens", "users"}
    if set(actual_user_tables) == auth_only_tables and missing_tables:
        likely_issue = (
            "Database appears to have been stamped or advanced to a later auth-only repair "
            "revision before the initial ERP schema revisions were applied."
        )
    elif missing_tables and current_revisions:
        likely_issue = (
            "Alembic version is present, but schema is incomplete for the checked revision. "
            "This usually means the database was stamped incorrectly or migrations were run "
            "against a different database."
        )
    elif missing_tables:
        likely_issue = "Schema is incomplete and Alembic version tracking is absent or empty."
    else:
        likely_issue = "Schema tables match the SQLAlchemy models."

    return SchemaReport(
        database_url_masked=_masked_database_url(),
        alembic_heads=heads,
        current_revisions=current_revisions,
        actual_tables=actual_user_tables,
        expected_tables=expected_tables,
        missing_tables=missing_tables,
        unexpected_tables=unexpected_tables,
        missing_table_migrations=missing_table_migrations,
        likely_issue=likely_issue,
    )


def _print_text(report: SchemaReport) -> None:
    print("Schema diagnostics")
    print(f"database_url={report.database_url_masked}")
    print(f"alembic_heads={','.join(report.alembic_heads) or 'none'}")
    print(f"current_revisions={','.join(report.current_revisions) or 'none'}")
    print(f"actual_table_count={len(report.actual_tables)}")
    print(f"expected_table_count={len(report.expected_tables)}")
    print(f"missing_table_count={len(report.missing_tables)}")
    print(f"likely_issue={report.likely_issue}")
    print("")
    print("actual_tables:")
    for table_name in report.actual_tables:
        print(f"  {table_name}")
    print("")
    print("missing_tables:")
    if not report.missing_tables:
        print("  none")
    else:
        for table_name in report.missing_tables:
            revisions = ", ".join(report.missing_table_migrations.get(table_name, [])) or "unknown"
            print(f"  {table_name}  <- {revisions}")
    print("")
    print("unexpected_tables:")
    if not report.unexpected_tables:
        print("  none")
    else:
        for table_name in report.unexpected_tables:
            print(f"  {table_name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only production-safe schema diagnostics for Alembic and ORM tables.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report as JSON",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    report = asyncio.run(build_report())
    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
