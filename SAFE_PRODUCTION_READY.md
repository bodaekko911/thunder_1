# Safe Production Readiness

This file summarizes the safe, incremental production-hardening work completed without intentionally changing successful response formats.

## Changes Applied

- Added baseline pytest suite for health, auth guardrails, permission logic, and critical expense logic.
- Added explicit rate limits for sensitive endpoints:
  - `/auth/login`
  - `/auth/refresh`
  - `/users/api/users/{user_id}/reset-password`
  - `/users/api/change-password`
- Removed silent global default rate limiting so ordinary endpoints are not throttled by default.
- Added Docker app healthchecks in both development and production compose files using `/health/live`.
- Kept current router structure intact and verified active registration uses `expenses_refactored`.
- Kept Alembic review and startup migration checks in place from earlier hardening work.

## Verification

Commands run:

```powershell
python -m pytest -q
python -m compileall app tests alembic
python -m alembic history
python -c "from app.main import app; print(app.title)"
```

Observed result:

- `10 passed` in pytest
- app imports successfully
- Alembic revision visible
- no endpoint response shape changes introduced by this pass

## Remaining Manual Production Step

- Existing databases should be stamped or migrated to Alembic revision `20260412_0001` before enabling strict migration enforcement in production.

## Safety Notes

- no API paths were renamed
- no response payload contracts were intentionally changed
- no schema changes were applied automatically in this pass
- fixes were additive or narrowly scoped for easy rollback
