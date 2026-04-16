import json
from datetime import date

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.sql import func

from app.database import Base


class AssistantSession(Base):
    __tablename__ = "assistant_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    channel = Column(String(50), nullable=False, default="dashboard", index=True)
    last_intent = Column(String(100), nullable=True)
    last_date_from = Column(Date, nullable=True)
    last_date_to = Column(Date, nullable=True)
    last_entity_ids = Column(Text, nullable=True)
    last_comparison_baseline = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def set_last_entity_ids(self, entity_ids: list[int] | None) -> None:
        self.last_entity_ids = json.dumps(entity_ids or [])

    def get_last_entity_ids(self) -> list[int]:
        if not self.last_entity_ids:
            return []
        try:
            payload = json.loads(self.last_entity_ids)
        except (TypeError, ValueError):
            return []
        return [int(value) for value in payload if isinstance(value, int)]

    def set_last_date_range(self, date_from: date | None, date_to: date | None) -> None:
        self.last_date_from = date_from
        self.last_date_to = date_to


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("assistant_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    message_text = Column(Text, nullable=False)
    intent = Column(String(100), nullable=True)
    parameters_json = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AssistantFeedback(Base):
    __tablename__ = "assistant_feedback"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("assistant_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id = Column(Integer, ForeignKey("assistant_messages.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    rating = Column(SmallInteger, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
