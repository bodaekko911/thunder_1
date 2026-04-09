"""
Shared activity logging utility.
Call record() from any router to write to the activity_logs table.
"""
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


# Re-export the model so it stays in one place
class ActivityLog(Base):
    __tablename__ = "activity_logs"
    __table_args__ = {"extend_existing": True}
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, nullable=True)
    user_name   = Column(String(150))
    user_role   = Column(String(50))
    action      = Column(String(100))
    module      = Column(String(50))
    description = Column(Text)
    ref_type    = Column(String(50))
    ref_id      = Column(String(50))
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


def record(
    db:          Session,
    module:      str,
    action:      str,
    description: str,
    user=None,
    ref_type:    str = None,
    ref_id:      str = None,
):
    """
    Write one activity log entry and flush (caller must commit).
    user can be a User ORM object or None.
    """
    entry = ActivityLog(
        user_id     = user.id   if user else None,
        user_name   = user.name if user else "System",
        user_role   = user.role if user else "system",
        action      = action,
        module      = module,
        description = description,
        ref_type    = ref_type,
        ref_id      = str(ref_id) if ref_id is not None else None,
    )
    db.add(entry)
    # Don't commit here — caller commits after their own changes