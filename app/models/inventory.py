from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class StockLocation(Base):
    __tablename__ = "stock_locations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    code = Column(String(40), nullable=True, unique=True, index=True)
    location_type = Column(String(30), nullable=False, default="warehouse")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LocationStock(Base):
    __tablename__ = "location_stocks"
    __table_args__ = (
        UniqueConstraint("location_id", "product_id", name="uq_location_stocks_location_product"),
    )

    id = Column(Integer, primary_key=True, index=True)
    location_id = Column(Integer, ForeignKey("stock_locations.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    qty = Column(Numeric(12, 3), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    location = relationship("StockLocation")
    product = relationship("Product")


class StockTransfer(Base):
    __tablename__ = "stock_transfers"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    source_location_id = Column(Integer, ForeignKey("stock_locations.id"), nullable=False, index=True)
    destination_location_id = Column(Integer, ForeignKey("stock_locations.id"), nullable=False, index=True)
    qty = Column(Numeric(12, 3), nullable=False)
    note = Column(Text)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product")
    source_location = relationship("StockLocation", foreign_keys=[source_location_id])
    destination_location = relationship("StockLocation", foreign_keys=[destination_location_id])
    user = relationship("User")


class StockMove(Base):
    __tablename__ = "stock_moves"

    id         = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    type       = Column(String(20), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)
    qty_before = Column(Numeric(12, 3))
    qty_after  = Column(Numeric(12, 3))
    ref_type   = Column(String(30))
    ref_id     = Column(Integer)
    note       = Column(Text)
    user_id    = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product", back_populates="stock_moves")
    user    = relationship("User")
