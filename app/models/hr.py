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
    farm_id     = Column(Integer, ForeignKey("farms.id"), nullable=True, index=True)
    is_active   = Column(Boolean, default=True)
    attendance_auto_status = Column(String(20), nullable=False, default="present", server_default="present")
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    attendance = relationship("Attendance", back_populates="employee")
    payrolls   = relationship("Payroll", back_populates="employee")
    loans      = relationship("EmployeeLoan", back_populates="employee")
    loan_repayments = relationship("EmployeeLoanRepayment", back_populates="employee")
    payroll_deductions = relationship("EmployeePayrollDeduction", back_populates="employee")
    farm       = relationship("Farm", back_populates="employees")


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
    loan_deductions = Column(Numeric(12, 2), default=0, server_default="0")
    day_deduction_days = Column(Numeric(8, 2), default=0, server_default="0")
    day_deductions = Column(Numeric(12, 2), default=0, server_default="0")
    manual_deductions = Column(Numeric(12, 2), default=0, server_default="0")
    net_salary  = Column(Numeric(12, 2))
    paid        = Column(Boolean, default=False)
    days_worked  = Column(Integer, nullable=True)
    working_days = Column(Integer, nullable=True)
    paid_at      = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    employee = relationship("Employee", back_populates="payrolls")
    loan_repayments = relationship("EmployeeLoanRepayment", back_populates="payroll")
    payroll_deductions = relationship("EmployeePayrollDeduction", back_populates="payroll")


class EmployeeLoan(Base):
    __tablename__ = "employee_loans"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    loan_date   = Column(Date, nullable=False)
    amount      = Column(Numeric(12, 2), nullable=False)
    description = Column(Text, nullable=True)
    status      = Column(String(20), nullable=False, default="open", server_default="open")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    employee = relationship("Employee", back_populates="loans")
    repayments = relationship("EmployeeLoanRepayment", back_populates="loan")
    created_by = relationship("User", foreign_keys=[created_by_user_id])


class EmployeeLoanRepayment(Base):
    __tablename__ = "employee_loan_repayments"

    id          = Column(Integer, primary_key=True, index=True)
    loan_id     = Column(Integer, ForeignKey("employee_loans.id"), nullable=False, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    payroll_id  = Column(Integer, ForeignKey("payroll.id"), nullable=True, index=True)
    repayment_date = Column(Date, nullable=False)
    amount      = Column(Numeric(12, 2), nullable=False)
    note        = Column(Text, nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    loan = relationship("EmployeeLoan", back_populates="repayments")
    employee = relationship("Employee", back_populates="loan_repayments")
    payroll = relationship("Payroll", back_populates="loan_repayments")
    created_by = relationship("User", foreign_keys=[created_by_user_id])


class EmployeePayrollDeduction(Base):
    __tablename__ = "employee_payroll_deductions"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    payroll_id  = Column(Integer, ForeignKey("payroll.id"), nullable=True, index=True)
    period      = Column(String(7), nullable=True, index=True)
    deduction_date = Column(Date, nullable=True)
    type        = Column(String(30), nullable=False)
    days        = Column(Numeric(8, 2), nullable=True)
    daily_rate  = Column(Numeric(12, 2), nullable=True)
    amount      = Column(Numeric(12, 2), nullable=False)
    note        = Column(Text, nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    employee = relationship("Employee", back_populates="payroll_deductions")
    payroll = relationship("Payroll", back_populates="payroll_deductions")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
