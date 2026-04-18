"""
Shared helpers used by both app/routers/b2b.py and the B2B sales import service.
Extracted to avoid duplication and to allow the import service to pass
created_at / ref_id that the router doesn't need.
"""
from decimal import Decimal
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.sql import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.b2b import B2BInvoice, Consignment
from app.models.accounting import Account, Journal, JournalEntry


async def post_journal(
    db: AsyncSession,
    description: str,
    ref_type: str,
    entries: list,
    user_id: Optional[int] = None,
    created_at: Optional[datetime] = None,
    ref_id: Optional[int] = None,
) -> None:
    journal = Journal(ref_type=ref_type, description=description, user_id=user_id)
    if created_at is not None:
        journal.created_at = created_at
    if ref_id is not None:
        journal.ref_id = ref_id
    db.add(journal)
    await db.flush()
    for code, debit, credit in entries:
        _r = await db.execute(select(Account).where(Account.code == code))
        acc = _r.scalar_one_or_none()
        if acc:
            db.add(JournalEntry(
                journal_id=journal.id, account_id=acc.id,
                debit=debit, credit=credit,
            ))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))


async def seed_deferred_revenue(db: AsyncSession) -> None:
    """Ensure account 2200 Deferred Revenue exists."""
    _r = await db.execute(select(Account).where(Account.code == "2200"))
    if _r.scalar_one_or_none() is None:
        db.add(Account(
            code="2200", name="Deferred Revenue",
            account_type="liability", balance=Decimal("0"),
        ))
        await db.commit()


async def next_b2b_number(db: AsyncSession) -> str:
    _r = await db.execute(select(sa_func.max(B2BInvoice.id)))
    max_id = _r.scalar() or 0
    return f"B2B-{str(max_id + 1).zfill(5)}"


async def next_cons_number(db: AsyncSession) -> str:
    _r = await db.execute(select(sa_func.max(Consignment.id)))
    max_id = _r.scalar() or 0
    return f"CONS-{str(max_id + 1).zfill(4)}"
