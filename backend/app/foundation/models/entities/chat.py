from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_user_conv", "user_id", "conversation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), default=None)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class LlmAuditEvent(Base):
    __tablename__ = "llm_audit_events"
    __table_args__ = (Index("ix_llm_audit_events_created_state", "created_at", "retention_state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    agent: Mapped[str] = mapped_column(String(80), default="chat")
    purpose: Mapped[str] = mapped_column(String(120), default="general")
    prompt_json: Mapped[str] = mapped_column(Text, default="{}")
    response_json: Mapped[str] = mapped_column(Text, default="{}")
    redacted_prompt_json: Mapped[str | None] = mapped_column(Text)
    redacted_response_json: Mapped[str | None] = mapped_column(Text)
    retention_state: Mapped[str] = mapped_column(String(24), default="raw", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
