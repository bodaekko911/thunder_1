from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, ForeignKey, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Employee(Base):
    __tablename__ = "employees"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(150), nullable=False)
    phone       = Column(String(30))
    position    = Column(String(100))
    department  = Column(String(100))
    hire_date   = Column(Date)
    base_salary = Column(Numeric(12, 2), default=0)
    is_active   = Column(Boolean, default=True)
    attendance_auto_status = Column(String(20), nullable=False, default="present", server_default="present")
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    attendance = relationship("Attendance", back_populates="employee")
    payrolls   = relationship("Payroll", back_populates="employee")


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("employee_id", "date", name="uq_attendance_employee_date"),
    )

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date        = Column(Date, nullable=False)
    status      = Column(String(20), default="present")
    note        = Column(Text)

    employee = relationship("Employee", back_populates="attendance")


class Payroll(Base):
    __tablename__ = "payroll"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    period      = Column(String(7), nullable=False)
    base_salary = Column(Numeric(12, 2))
    bonuses     = Column(Numeric(12, 2), default=0)
    deductions  = Column(Numeric(12, 2), default=0)
    net_salary  = Column(Numeric(12, 2))
    paid        = Column(Boolean, default=False)
    days_worked  = Column(Integer, nullable=True)
    working_days = Column(Integer, nullable=True)
    paid_at      = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    employee = relationship("Employee", back_populates="payrolls")
