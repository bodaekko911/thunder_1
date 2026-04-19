from sqlalchemy import Column, Integer, String, Text, Numeric, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Product(Base):
    __tablename__ = "products"

    id        = Column(Integer, primary_key=True, index=True)
    sku       = Column(String(80), unique=True, index=True, nullable=False)
    name      = Column(String(200), nullable=False, index=True)
    price     = Column(Numeric(12, 2), nullable=False)
    cost      = Column(Numeric(12, 2), default=0)
    stock     = Column(Numeric(12, 3), default=0)
    min_stock = Column(Numeric(12, 3), default=5)
    reorder_level = Column(Numeric(12, 3), nullable=True)
    reorder_qty = Column(Numeric(12, 3), nullable=True)
    preferred_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    unit      = Column(String(30), default="pcs")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    category  = Column(String(100), nullable=True)
    item_type = Column(String(20),  default="finished")
    created_by_import_batch = Column(String(64), nullable=True, index=True)

    invoice_items  = relationship("InvoiceItem", back_populates="product")
    purchase_items = relationship("PurchaseItem", back_populates="product")
    stock_moves    = relationship("StockMove", back_populates="product")
    preferred_supplier = relationship("Supplier", foreign_keys=[preferred_supplier_id])
