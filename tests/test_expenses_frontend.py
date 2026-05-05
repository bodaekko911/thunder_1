from pathlib import Path


def test_expenses_load_expenses_reports_api_failures() -> None:
    source = Path("app/routers/expenses.py").read_text(encoding="utf-8")

    assert "if (!response.ok)" in source
    assert "const body = await response.text()" in source
    assert "JSON.parse(body)" in source
    assert "if (!Array.isArray(data))" in source
    assert 'console.error("Failed to load expenses"' in source
    assert "Could not load expenses (${response.status})" in source
    assert "No expenses found in this database." in source


def test_expenses_load_expenses_renders_rows_defensively() -> None:
    source = Path("app/routers/expenses.py").read_text(encoding="utf-8")

    assert 'const paymentMethod = String(e.payment_method || "cash")' in source
    assert "const amount = Number(e.amount || 0)" in source
    assert "Number.isFinite(amount) ? amount.toFixed(2)" in source
    assert 'const ref = e.ref_number || "#" + e.id' in source
    assert 'const category = e.category || "—"' in source
    assert 'const expenseDate = e.expense_date || "—"' in source
    assert 'paymentMethod.replace(/_/g, " ")' in source
    assert "URLSearchParams" in source


def test_expenses_boot_does_not_block_table_on_secondary_loaders() -> None:
    source = Path("app/routers/expenses.py").read_text(encoding="utf-8")

    assert "loadCategories().catch" in source
    assert "loadSummary().catch" in source
    assert "loadFarmsDropdown().catch" in source
    assert "await loadExpenses();" in source
    assert "Promise.all([loadCategories(), loadSummary(), loadExpenses(), loadFarmsDropdown()])" not in source
