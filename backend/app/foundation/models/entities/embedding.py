from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, String, Text


class Embedding(Base):
    """pgvector embeddings for RAG corpus (Phase 6)."""

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(255), index=True)
    item_type: Mapped[str] = mapped_column(
        String(64), index=True
    )  # obsidian_note, financial_corpus, dossier, audit_event
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )


class FinAgentRun(Base):
    """FinAgent execution record: Budget, Substitution, Explainer runs (Phase 6)."""

    __tablename__ = "finagent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    agent: Mapped[str] = mapped_column(String(64), index=True)  # budget, substitution, explainer
    query: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )
