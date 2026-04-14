# ERP System

## Local Testing

`pytest` is self-contained for local development. The test suite applies its own development-safe defaults before importing the app, so you do not need to manually export `SECRET_KEY`, `DATABASE_URL`, or other required settings first.

Run:

```powershell
python -m pytest -q
```

## Migrations

Alembic migrations are schema-only. They do not create default users or seed categories.

Run:

```powershell
python -m alembic upgrade head
```

Inspect heads/history:

```powershell
python -m alembic heads
python -m alembic history
```

## Optional Bootstrap Data

Initial data setup is explicit and idempotent. It is not run during app startup or during Alembic migrations.

Create the default admin user and default expense categories:

```powershell
python -m app.bootstrap.init_data --all
```

Run only one part:

```powershell
python -m app.bootstrap.init_data --admin
python -m app.bootstrap.init_data --expense-categories
```

When `APP_ENV=production`, add `--yes`:

```powershell
python -m app.bootstrap.init_data --all --yes
```

The bootstrap command is safe to run multiple times. Existing records are left in place, and only missing defaults are created.

## Optional Demo Data

To insert a small demo dataset for manual testing, run:

```powershell
python -m app.bootstrap.seed_demo_data
```

When `APP_ENV=production`, add `--yes`:

```powershell
python -m app.bootstrap.seed_demo_data --yes
```

This command is manual-only and idempotent. It creates a small sample set of customers, products, invoices, and expenses only when those demo records are missing.

## Data Diagnostics

To inspect whether production is missing business data or reference data, run:

```powershell
python -m app.bootstrap.diagnose_data
```

For machine-readable output:

```powershell
python -m app.bootstrap.diagnose_data --json
```

This command is read-only. It does not seed or modify the database.
