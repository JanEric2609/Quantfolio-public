from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import DateTime, ForeignKey, Integer, Text, String

from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk

from datetime import datetime


class CompetitionRun(Base):
    __tablename__ = "competition_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    portfolio_a_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
    )
    portfolio_b_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(120), default="Dual Competition")
    cadence_days: Mapped[int] = mapped_column(Integer, default=3)
    current_round: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="init")
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
        onupdate=now_utc,
    )

    decisions: Mapped[list["CompetitionDecision"]] = relationship(
        back_populates="run",
    )


class CompetitionDecision(Base):
    __tablename__ = "competition_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("competition_runs.id", ondelete="CASCADE"),
    )
    round_number: Mapped[int] = mapped_column(Integer, index=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
    )
    council_result_json: Mapped[str] = mapped_column(Text, default="{}")
    debate_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    blm_results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_trades_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    winner: Mapped[bool] = mapped_column(default=False)
    errors: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
    )

    run: Mapped[CompetitionRun] = relationship(back_populates="decisions", lazy="selectin")
