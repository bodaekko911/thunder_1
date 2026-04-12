# Safe Audit

Audit goal: identify production-hardening issues without changing behavior unnecessarily.

## Critical

- Database schema is not yet stamped in Alembic on existing environments.
  Impact: startup migration checks can detect drift, but existing databases still appear as legacy unversioned until explicitly stamped.
  Status: detected and documented. No destructive schema action was taken automatically.

- Authentication-sensitive endpoints needed explicit throttling instead of relying on global default rate limits.
  Impact: global limits can throttle ordinary traffic while still leaving some sensitive endpoints without targeted protection.
  Status: fixed safely by moving rate limiting to sensitive endpoints only.

## High

- Test coverage was too thin to protect current behavior during hardening.
  Impact: regressions could slip into health/auth/business logic.
  Status: baseline suite added.

- Docker app service had no explicit application-level healthcheck.
  Impact: container orchestration could treat a bad process as healthy if the container stayed alive.
  Status: fixed safely with `/health/live` checks.

- Expenses has both legacy and refactored router files in the codebase.
  Impact: maintenance confusion and risk of accidental registration drift.
  Status: current active router registration points to `expenses_refactored`; no behavior change required right now.

## Low

- Some routers still mix route logic and business logic directly.
  Impact: maintainability and testability issues, but not always immediate production failures.
  Status: partially improved in expenses; broader rollout should stay incremental.

- Some modules use mixed auth patterns: cookie-based browser auth in some places, authorization-header parsing in others.
  Impact: inconsistency can confuse future changes.
  Status: no behavior change applied yet to avoid breaking existing clients.

- Pytest cache could not be written in this workspace on Windows.
  Impact: harmless warnings only.
  Status: no runtime impact.

## What Was Verified

- active router registry in `app/routers/__init__.py`
- auth and permission enforcement paths
- current rate limiting configuration
- Docker dev/prod compose health and dependency wiring
- Alembic script discovery and revision visibility

## Safe Fix Strategy

- prefer additive tests before behavior changes
- prefer config-based or middleware-level hardening over endpoint rewrites
- avoid schema changes unless required
- keep response payloads unchanged
