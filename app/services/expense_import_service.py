from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import openpyxl
from fastapi import HTTPException
from openpyxl.utils.datetime import from_excel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import ExpenseCategory
from app.models.farm import Farm
from app.schemas.expense import ExpenseCategoryCreate, ExpenseCreate
from app.services.expense_service import create_category, create_expense_entry


@dataclass
class ParsedExpenseRow:
    excel_row: int
    category_name: str
    amount: Decimal
    expense_date: date
    farm_id: int | None
    farm_name: str | None
    general_expense: bool


def _normalize_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    return " ".join(text.split())


def _normalize_name(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _find_header_index(headers: list[str], aliases: list[str]) -> int | None:
    normalized = [_normalize_key(header) for header in headers]
    alias_set = {_normalize_key(alias) for alias in aliases}
    for idx, header in enumerate(normalized):
        if header in alias_set:
            return idx + 1
    return None


def _parse_amount(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value)).quantize(Decimal("0.01"))

    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace(",", "").replace("$", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _parse_date_value(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = from_excel(value)
        except Exception:
            parsed = None
        if isinstance(parsed, datetime):
            return parsed.date()
        if isinstance(parsed, date):
            return parsed

    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


async def _load_category_map(db: AsyncSession) -> dict[str, ExpenseCategory]:
    result = await db.execute(
        select(ExpenseCategory).where(ExpenseCategory.is_active == "1")
    )
    categories = result.scalars().all()
    return {_normalize_name(category.name).lower(): category for category in categories}


async def _load_farm_map(db: AsyncSession) -> dict[str, Farm]:
    result = await db.execute(select(Farm).where(Farm.is_active == 1))
    farms = result.scalars().all()
    return {_normalize_name(farm.name).lower(): farm for farm in farms}


def _empty_row(values: list[object]) -> bool:
    return all(str(value or "").strip() == "" for value in values)


async def import_expenses(
    *,
    db: AsyncSession,
    workbook_bytes: bytes,
    filename: str,
    current_user,
    dry_run: bool = True,
) -> dict:
    workbook = openpyxl.load_workbook(io.BytesIO(workbook_bytes), data_only=True)
    sheet = workbook.active
    headers = [str(sheet.cell(1, col).value or "") for col in range(1, sheet.max_column + 1)]

    col_category = _find_header_index(headers, ["Category", "Expense Category"])
    col_amount = _find_header_index(headers, ["Amount", "Expense Amount", "Value"])
    col_farm = _find_header_index(headers, ["Farm", "Farm Name"])
    col_date = _find_header_index(headers, ["Date", "Expense Date"])

    missing = []
    if not col_category:
        missing.append("Category")
    if not col_amount:
        missing.append("Amount")
    if not col_date:
        missing.append("Date")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required column(s): {', '.join(missing)}",
        )

    category_map = await _load_category_map(db)
    farm_map = await _load_farm_map(db)
    rows: list[ParsedExpenseRow] = []
    errors: list[dict] = []
    missing_categories: dict[str, str] = {}
    resolved_farm_rows = 0
    general_expense_rows = 0
    total_amount = Decimal("0.00")
    date_values: list[date] = []

    for row_idx in range(2, sheet.max_row + 1):
        row_values = [sheet.cell(row_idx, col).value for col in range(1, sheet.max_column + 1)]
        if _empty_row(row_values):
            continue

        raw_category = sheet.cell(row_idx, col_category).value if col_category else None
        raw_amount = sheet.cell(row_idx, col_amount).value if col_amount else None
        raw_farm = sheet.cell(row_idx, col_farm).value if col_farm else None
        raw_date = sheet.cell(row_idx, col_date).value if col_date else None

        category_name = _normalize_name(raw_category)
        amount = _parse_amount(raw_amount)
        expense_date = _parse_date_value(raw_date)
        farm_name = _normalize_name(raw_farm)

        row_errors: list[str] = []
        if not category_name:
            row_errors.append("Category is required")
        if amount is None:
            row_errors.append("Amount is required and must be numeric")
        elif amount <= 0:
            row_errors.append("Amount must be greater than 0")
        if expense_date is None:
            row_errors.append("Date is required and must be a valid date")

        category_key = category_name.lower()
        category = category_map.get(category_key) if category_name else None
        if category_name and category is None and category_key not in missing_categories:
            missing_categories[category_key] = category_name

        farm = None
        general_expense = not farm_name
        if not general_expense:
            farm = farm_map.get(farm_name.lower())
            if farm is None:
                row_errors.append(f"Farm '{farm_name}' was not found")

        if row_errors:
            errors.append(
                {
                    "row": row_idx,
                    "category": category_name or "",
                    "amount": "" if raw_amount is None else str(raw_amount),
                    "farm": farm_name or "General Expense",
                    "date": "" if raw_date is None else str(raw_date),
                    "reason": "; ".join(row_errors),
                }
            )
            continue

        rows.append(
            ParsedExpenseRow(
                excel_row=row_idx,
                category_name=category_name,
                amount=amount,
                expense_date=expense_date,
                farm_id=farm.id if farm else None,
                farm_name=farm.name if farm else None,
                general_expense=general_expense,
            )
        )
        if general_expense:
            general_expense_rows += 1
        elif farm is not None:
            resolved_farm_rows += 1
        total_amount += amount
        date_values.append(expense_date)

    auto_created_categories: list[dict] = []
    if not dry_run and missing_categories:
        for category_name in missing_categories.values():
            created = await create_category(
                db,
                ExpenseCategoryCreate(name=category_name, description="Auto-created by Expenses import"),
            )
            auto_created_categories.append(created)
        category_map = await _load_category_map(db)

    expenses_created = 0
    if not dry_run:
        for row in rows:
            category = category_map.get(row.category_name.lower())
            if category is None:
                errors.append(
                    {
                        "row": row.excel_row,
                        "category": row.category_name,
                        "amount": str(row.amount),
                        "farm": row.farm_name or "General Expense",
                        "date": row.expense_date.isoformat(),
                        "reason": f"Category '{row.category_name}' could not be created",
                    }
                )
                continue
            payload = ExpenseCreate(
                category_id=category.id,
                expense_date=row.expense_date.isoformat(),
                amount=float(row.amount),
                payment_method="cash",
                farm_id=row.farm_id,
            )
            await create_expense_entry(db, payload, current_user)
            expenses_created += 1

    categories_auto_created = len(auto_created_categories) if not dry_run else len(missing_categories)
    valid_rows = len(rows)
    summary = {
        "rows_read": valid_rows + len(errors),
        "rows_imported": valid_rows if dry_run else expenses_created,
        "rows_skipped": len(errors),
        "expenses_created": expenses_created,
        "expenses_would_create": valid_rows,
        "expense_records_created": expenses_created,
        "categories_auto_created": categories_auto_created,
        "farms_resolved": resolved_farm_rows,
        "general_expense_rows": general_expense_rows,
        "earliest_date": min(date_values).isoformat() if date_values else None,
        "latest_date": max(date_values).isoformat() if date_values else None,
        "total_amount": float(total_amount),
    }
    warnings = []
    if general_expense_rows:
        warnings.append(
            f"{general_expense_rows} row(s) will be recorded as General Expense because Farm was blank."
        )

    return {
        "ok": len(errors) == 0,
        "dry_run": dry_run,
        "filename": filename,
        "summary": summary,
        "errors": errors,
        "warnings": warnings,
        "auto_created_categories": [
            {"name": category["name"], "account_code": category.get("account_code")}
            for category in auto_created_categories
        ] if not dry_run else [
            {"name": category_name, "account_code": None}
            for category_name in missing_categories.values()
        ],
    }
