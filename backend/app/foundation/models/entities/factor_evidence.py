"""Evidence that may unlock money in the monthly plan (report Phases 3 and 4).

``FactorEvidenceCard``: one row per pre-registered factor strategy per study
run, written by ``app.lab.factor_premia``; the latest run decides whether the
factor tilt may receive money.

``EvidenceGateRun``: one row per run of ``app.lab.evidence_gate`` (DSR/PBO over
the mined scores); the latest run decides whether the stock-picking satellite
may receive money.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class FactorEvidenceCard(Base):
    """The outcome of one strategy's prior-informed test in one study run."""

    __tablename__ = "factor_evidence_cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    # Groups the cards of one study run; the latest run is the current one.
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    region: Mapped[str] = mapped_column(String(32))
    months: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    card_json: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)


class EvidenceGateRun(Base):
    """One DSR/PBO grading of the mined scores' satellite records."""

    __tablename__ = "evidence_gate_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    region: Mapped[str] = mapped_column(String(32), index=True)
    satellite_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # "<broker>:<model>" of each mined score that passed; empty while locked.
    unlocked_by: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    n_trials: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
