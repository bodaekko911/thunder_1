from pathlib import Path


def _source() -> str:
    return Path("app/routers/expenses.py").read_text(encoding="utf-8")


def test_expenses_load_expenses_reports_api_failures() -> None:
    source = _source()

    assert "if (!response.ok)" in source
    assert "const body = await response.text()" in source
    assert "JSON.parse(body)" in source
    assert "if (!Array.isArray(data))" in source
    assert 'console.error("Failed to load expenses"' in source
    assert "Could not load expenses (${response.status})" in source
    assert "No expenses found in this database." in source


def test_expenses_load_expenses_renders_rows_defensively() -> None:
    source = _source()

    assert 'const paymentMethod = String(e.payment_method || "cash")' in source
    assert "const amount = Number(e.amount || 0)" in source
    assert "Number.isFinite(amount) ? amount.toFixed(2)" in source
    assert 'const ref = e.ref_number || "#" + e.id' in source
    assert 'const category = e.category || "—"' in source
    assert 'const expenseDate = e.expense_date || "—"' in source
    assert 'paymentMethod.replace(/_/g, " ")' in source
    assert "URLSearchParams" in source


def test_expenses_boot_does_not_block_table_on_secondary_loaders() -> None:
    source = _source()

    assert "await Promise.allSettled([" in source
    assert "loadCategories()," in source
    assert "loadSummary()," in source
    assert "loadExpenses()," in source
    assert "loadFarmsDropdown()" in source
    assert "Promise.all([loadCategories(), loadSummary(), loadExpenses(), loadFarmsDropdown()])" not in source


def test_expenses_boot_has_no_dead_month_filter_dom_write() -> None:
    source = _source()

    assert 'id="month-filter"' not in source
    assert 'getElementById("month-filter")' not in source


def test_expenses_loaders_check_status_and_log_debug_shapes() -> None:
    source = _source()

    assert 'console.log("Loading expenses page data: categories")' in source
    assert 'console.log("Loading expenses page data: summary")' in source
    assert 'console.log("Loading expenses page data: expenses")' in source
    assert 'console.log("Loading expenses page data: farms")' in source
    assert "async function readJsonResponse(response, loaderName)" in source
    assert "if (!response.ok)" in source
    assert 'showInlineError("cat-list-body"' in source
    assert "Summary error" in source
    assert "Farm list unavailable" in source
