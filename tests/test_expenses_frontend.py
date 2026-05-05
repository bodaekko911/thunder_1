from pathlib import Path


def test_expenses_load_expenses_reports_api_failures() -> None:
    source = Path("app/routers/expenses.py").read_text(encoding="utf-8")

    assert "if (!response.ok)" in source
    assert 'console.error("Failed to load expenses"' in source
    assert "Could not load expenses (${response.status})" in source
    assert "No expenses found" in source
