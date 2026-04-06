from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Supplier(Base):
    __tablename__ = "suppliers"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(150), nullable=False, index=True)
    phone      = Column(String(30))
    email      = Column(String(150))
    address    = Column(Text)
    balance    = Column(Numeric(12, 2), default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchases = relationship("Purchase", back_populates="supplier")


class Purchase(Base):
    __tablename__ = "purchases"

    id              = Column(Integer, primary_key=True, index=True)
    purchase_number = Column(String(30), unique=True, index=True)
    supplier_id     = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"))
    status          = Column(String(20), default="received")
    subtotal        = Column(Numeric(12, 2), default=0)
    discount        = Column(Numeric(12, 2), default=0)
    total           = Column(Numeric(12, 2), default=0)
    notes           = Column(Text)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    supplier = relationship("Supplier", back_populates="purchases")
    user     = relationship("User")
    items    = relationship("PurchaseItem", back_populates="purchase",
                           cascade="all, delete-orphan")


class PurchaseItem(Base):
    __tablename__ = "purchase_items"

    id          = Column(Integer, primary_key=True, index=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=False)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty         = Column(Numeric(12, 3), nullable=False)
    unit_cost   = Column(Numeric(12, 2), nullable=False)
    total       = Column(Numeric(12, 2), nullable=False)

    purchase = relationship("Purchase", back_populates="items")
    product  = relationship("Product", back_populates="purchase_items")