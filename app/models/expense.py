from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class ExpenseCategory(Base):
    """
    User-defined expense categories, e.g. Water, Electricity, Gas, Rent.
    Each maps to a dedicated expense ledger account (code 5xxx).
    """
    __tablename__ = "expense_categories"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String(100), nullable=False, unique=True)
    account_code = Column(String(20), nullable=False)   # e.g. "5001", "5002"
    description  = Column(String(255), nullable=True)
    is_active    = Column(String(1), default="1")        # "1" active, "0" archived

    expenses = relationship("Expense", back_populates="category")


class Expense(Base):
    """
    A single expense transaction — one bill, one payment, one entry.
    Automatically posts a journal entry (Debit expense account, Credit cash/bank).
    """
    __tablename__ = "expenses"

    id              = Column(Integer, primary_key=True, index=True)
    ref_number      = Column(String(30), unique=True, index=True)  # EXP-00001
    category_id     = Column(Integer, ForeignKey("expense_categories.id"), nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"),              nullable=True)
    expense_date    = Column(Date, nullable=False)
    amount          = Column(Numeric(14, 2), nullable=False)
    payment_method  = Column(String(20), default="cash")  # cash | bank_transfer | card
    vendor          = Column(String(150), nullable=True)  # e.g. "Cairo Electric Co."
    description     = Column(Text, nullable=True)
    journal_id      = Column(Integer, ForeignKey("journals.id"), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    farm_id     = Column(Integer, ForeignKey("farms.id"), nullable=True)

    category = relationship("ExpenseCategory", back_populates="expenses")
    user     = relationship("User")
    journal  = relationship("Journal")
    farm     = relationship("Farm")