from pydantic import BaseModel, Field
from typing import Optional, List


class EmployeeCreate(BaseModel):
    name:        str = Field(..., min_length=1, max_length=200)
    phone:       Optional[str] = Field(None, max_length=50)
    position:    Optional[str] = Field(None, max_length=150)
    department:  Optional[str] = Field(None, max_length=150)
    hire_date:   Optional[str] = None
    base_salary: float = Field(0, ge=0)


class EmployeeUpdate(BaseModel):
    name:        Optional[str] = Field(None, min_length=1, max_length=200)
    phone:       Optional[str] = Field(None, max_length=50)
    position:    Optional[str] = Field(None, max_length=150)
    department:  Optional[str] = Field(None, max_length=150)
    base_salary: Optional[float] = Field(None, ge=0)
    is_active:   Optional[bool] = None


class AttendanceCreate(BaseModel):
    employee_id: int
    date:        str
    status:      str = Field("present", min_length=1, max_length=50)
    note:        Optional[str] = Field(None, max_length=500)


class PayrollRun(BaseModel):
    period:  str = Field(..., min_length=1, max_length=20)
    emp_ids: Optional[List[int]] = None


class PayrollUpdate(BaseModel):
    bonuses:    float = Field(0, ge=0)
    deductions: float = Field(0, ge=0)
    notes:      Optional[str] = Field(None, max_length=500)
