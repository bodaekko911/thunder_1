from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Invoice(Base):
    __tablename__ = "invoices"

    id             = Column(Integer, primary_key=True, index=True)
    invoice_number = Column(String(30), unique=True, index=True)
    customer_id    = Column(Integer, ForeignKey("customers.id"), nullable=False)
    user_id        = Column(Integer, ForeignKey("users.id"))
    status         = Column(String(20), default="paid")
    payment_method = Column(String(30), default="cash")
    subtotal       = Column(Numeric(12, 2), default=0)
    discount       = Column(Numeric(12, 2), default=0)
    total          = Column(Numeric(12, 2), default=0)
    notes          = Column(Text)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    import_batch_id = Column(String(64), nullable=True, index=True)

    customer = relationship("Customer", back_populates="invoices")
    user     = relationship("User")
    items    = relationship("InvoiceItem", back_populates="invoice",
                           cascade="all, delete-orphan")


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id         = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    sku        = Column(String(80))
    name       = Column(String(200))
    qty        = Column(Numeric(12, 3), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    total      = Column(Numeric(12, 2), nullable=False)

    invoice = relationship("Invoice", back_populates="items")
    product = relationship("Product", back_populates="invoice_items")