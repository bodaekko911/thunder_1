from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class RetailRefund(Base):
    __tablename__ = "retail_refunds"

    id             = Column(Integer, primary_key=True, index=True)
    refund_number  = Column(String(30), unique=True, index=True)
    invoice_id     = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    customer_id    = Column(Integer, ForeignKey("customers.id"), nullable=False)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=True)
    reason         = Column(String(200))
    refund_method  = Column(String(30), default="cash")   # cash | credit | exchange
    total          = Column(Numeric(12, 2), default=0)
    notes          = Column(Text)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    invoice  = relationship("Invoice")
    customer = relationship("Customer")
    user     = relationship("User")
    items    = relationship("RetailRefundItem", back_populates="refund",
                            cascade="all, delete-orphan")


class RetailRefundItem(Base):
    __tablename__ = "retail_refund_items"

    id         = Column(Integer, primary_key=True, index=True)
    refund_id  = Column(Integer, ForeignKey("retail_refunds.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    total      = Column(Numeric(12, 2), nullable=False)

    refund  = relationship("RetailRefund", back_populates="items")
    product = relationship("Product")