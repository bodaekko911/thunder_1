from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Account(Base):
    __tablename__ = "accounts"

    id        = Column(Integer, primary_key=True, index=True)
    code      = Column(String(20), unique=True, nullable=False)
    name      = Column(String(150), nullable=False)
    type      = Column(String(30), nullable=False)
    balance   = Column(Numeric(14, 2), default=0)
    parent_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)

    parent  = relationship("Account", remote_side=[id])
    entries = relationship("JournalEntry", back_populates="account")


class Journal(Base):
    __tablename__ = "journals"

    id          = Column(Integer, primary_key=True, index=True)
    ref_type    = Column(String(30))
    ref_id      = Column(Integer)
    description = Column(Text)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    user    = relationship("User")
    entries = relationship("JournalEntry", back_populates="journal",
                          cascade="all, delete-orphan")


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id         = Column(Integer, primary_key=True, index=True)
    journal_id = Column(Integer, ForeignKey("journals.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    debit      = Column(Numeric(14, 2), default=0)
    credit     = Column(Numeric(14, 2), default=0)
    note       = Column(String(255))

    journal = relationship("Journal", back_populates="entries")
    account = relationship("Account", back_populates="entries")