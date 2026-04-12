# Baseline Protection

This file captures the current behavior we are protecting before any larger production-hardening work.

## Endpoint Inventory

Source of truth:

```powershell
rg -n "^@router\.(get|post|put|delete|patch)\(" app\routers -S
```

Registered router modules:

- `auth`
- `home`
- `pos`
- `import_data`
- `dashboard`
- `products`
- `customers`
- `suppliers`
- `inventory`
- `hr`
- `accounting`
- `production`
- `b2b`
- `farm`
- `reports`
- `users`
- `refunds`
- `expenses_refactored`
- `audit_log`

Current route groups:

- `auth`: `/`, `/auth/login`, `/auth/me`, `/auth/permissions/catalog`, `/auth/register`, `/auth/logout`, `/auth/refresh`
- `home`: `/home`
- `pos`: `/products-cache`, `/search-products`, `/customers`, `/invoice`, `/unpaid-invoices`, `/invoice/{invoice_id}/collect`, `/invoice/{invoice_id}`, `/pos-sw.js`, `/pos`
- `import`: `/import/api/preview`, `/import/api/products`, `/import/api/stock`, `/import/api/customers`, `/import/`
- `dashboard`: `/dashboard/data`, `/dashboard`
- `products`: `/products/api/next-sku`, `/products/api/categories`, `/products/api/list`, `/products/api/add`, `/products/api/edit/{product_id}`, `/products/api/delete/{product_id}`, `/products/`
- `customers`: `/customers-mgmt/api/list`, `/customers-mgmt/api/invoices/{customer_id}`, `/customers-mgmt/api/profile/{customer_id}`, `/customers-mgmt/api/add`, `/customers-mgmt/api/edit/{customer_id}`, `/customers-mgmt/api/delete/{customer_id}`, `/customers-mgmt/profile/{customer_id}`, `/customers-mgmt/`
- `suppliers`: `/suppliers/api/list`, `/suppliers/api/add`, `/suppliers/api/edit/{supplier_id}`, `/suppliers/api/delete/{supplier_id}`, `/suppliers/api/purchases`, `/suppliers/api/purchase/{purchase_id}`, `/suppliers/api/purchase/create`, `/suppliers/api/products-list`, `/suppliers/`
- `inventory`: `/inventory/api/stock`, `/inventory/api/moves`, `/inventory/export/moves`, `/inventory/api/adjust`, `/inventory/api/summary`, `/inventory/`
- `hr`: `/hr/api/employees`, `/hr/api/attendance`, `/hr/api/payroll`, `/hr/api/summary`, payroll mutation endpoints, `/hr/`
- `accounting`: `/accounting/api/accounts`, `/accounting/api/journals`, `/accounting/api/trial-balance`, `/accounting/api/profit-loss`, B2B accounting endpoints, `/accounting/`
- `production`: recipes, batches, spoilage, lists, `/production/`
- `b2b`: clients, invoices, consignments, refunds, statements, printable pages, `/b2b/`
- `farm`: farms, deliveries, weather logs, stats, `/farm/`
- `reports`: reporting APIs and export endpoints, `/reports/`
- `users`: logs, user CRUD, reset/change password, `/users/`
- `refunds`: POS refund APIs and print page, `/refunds/`
- `expenses`: categories, list, summary, add/edit/delete, cost allocation, `/expenses/`
- `audit-log`: `/audit-log/data`, `/audit-log/meta`, `/audit-log/`
- global health endpoints from app factory: `/health/live`, `/health/ready`, `/health`

## Baseline Tests Added

Test files:

- `tests/test_main_endpoints.py`
- `tests/test_auth_endpoints.py`
- `tests/test_permissions.py`
- `tests/test_expense_service.py`

Covered behavior:

- health and readiness endpoints return current success shape
- auth-protected profile endpoint rejects unauthenticated access
- refresh endpoint rejects requests without refresh cookie
- permission normalization and permission checks behave consistently
- critical expense service rules reject invalid dates and prevent unsafe category archival

## Baseline Verification

Command:

```powershell
python -m pytest -q
```

Current result:

- `10 passed`

Notes:

- Tests are intentionally lightweight and additive.
- They protect response shapes and key guardrails without rewriting app behavior.
