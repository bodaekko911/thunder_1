from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


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