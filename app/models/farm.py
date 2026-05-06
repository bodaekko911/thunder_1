from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text, Date
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Farm(Base):
    __tablename__ = "farms"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(150), nullable=False, unique=True)
    location   = Column(String(200))
    notes      = Column(Text)
    is_active  = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    deliveries   = relationship("FarmDelivery", back_populates="farm")
    weather_logs = relationship("WeatherLog", back_populates="farm")
    employees    = relationship("Employee", back_populates="farm")


class FarmDelivery(Base):
    __tablename__ = "farm_deliveries"

    id             = Column(Integer, primary_key=True, index=True)
    delivery_number= Column(String(30), unique=True, index=True)
    farm_id        = Column(Integer, ForeignKey("farms.id"), nullable=False)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=True)
    delivery_date  = Column(Date, nullable=False)
    received_by    = Column(String(150))
    quality_notes  = Column(Text)
    notes          = Column(Text)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    farm  = relationship("Farm", back_populates="deliveries")
    user  = relationship("User")
    items = relationship("FarmDeliveryItem", back_populates="delivery",
                         cascade="all, delete-orphan")


class FarmDeliveryItem(Base):
    __tablename__ = "farm_delivery_items"

    id          = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("farm_deliveries.id"), nullable=False)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty         = Column(Numeric(12, 3), nullable=False)
    unit        = Column(String(30))
    notes       = Column(String(255))

    delivery = relationship("FarmDelivery", back_populates="items")
    product  = relationship("Product")


class WeatherLog(Base):
    __tablename__ = "weather_logs"

    id           = Column(Integer, primary_key=True, index=True)
    farm_id      = Column(Integer, ForeignKey("farms.id"), nullable=False)
    log_date     = Column(Date, nullable=False)
    temp_min     = Column(Numeric(5, 1), nullable=True)   # °C
    temp_max     = Column(Numeric(5, 1), nullable=True)   # °C
    rainfall_mm  = Column(Numeric(7, 2), nullable=True)
    humidity_pct = Column(Numeric(5, 1), nullable=True)   # %
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    farm = relationship("Farm", back_populates="weather_logs")
