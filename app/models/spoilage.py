from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text, Date
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class SpoilageRecord(Base):
    __tablename__ = "spoilage_records"

    id             = Column(Integer, primary_key=True, index=True)
    ref_number     = Column(String(30), unique=True, index=True)
    product_id     = Column(Integer, ForeignKey("products.id"), nullable=False)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=True)
    qty            = Column(Numeric(12, 3), nullable=False)
    spoilage_date  = Column(Date, nullable=False)
    reason         = Column(String(100))
    farm_id        = Column(Integer, ForeignKey("farms.id"), nullable=True)
    notes          = Column(Text)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product")
    farm    = relationship("Farm")
    user    = relationship("User")
