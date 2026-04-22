from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.accounting import Journal
from app.routers import accounting as accounting_router


def test_apply_date_range_uses_inclusive_start_and_exclusive_next_day_end() -> None:
    statement = accounting_router._apply_date_range(
        select(Journal),
        Journal.created_at,
        date(2026, 4, 2),
        date(2026, 4, 3),
    )

    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "journals.created_at >= '2026-04-02 00:00:00'" in compiled
    assert "journals.created_at < '2026-04-04 00:00:00'" in compiled
    assert "date(journals.created_at)" not in compiled


def test_apply_date_range_allows_open_ended_ranges() -> None:
    from_only = accounting_router._apply_date_range(
        select(Journal),
        Journal.created_at,
        date(2026, 4, 2),
        None,
    )
    to_only = accounting_router._apply_date_range(
        select(Journal),
        Journal.created_at,
        None,
        date(2026, 4, 3),
    )

    from_sql = str(from_only.compile(compile_kwargs={"literal_binds": True}))
    to_sql = str(to_only.compile(compile_kwargs={"literal_binds": True}))

    assert "journals.created_at >= '2026-04-02 00:00:00'" in from_sql
    assert "journals.created_at < '2026-04-04 00:00:00'" not in from_sql
    assert "journals.created_at < '2026-04-04 00:00:00'" in to_sql
    assert "journals.created_at >=" not in to_sql


def test_apply_date_range_rejects_invalid_ranges() -> None:
    with pytest.raises(HTTPException) as exc_info:
        accounting_router._apply_date_range(
            select(Journal),
            Journal.created_at,
            date(2026, 4, 4),
            date(2026, 4, 3),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "From date cannot be after To date"
