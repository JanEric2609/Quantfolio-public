"""Global experiment ledger (ADR 0015 — Honest Measurement Rebuild, Phase 1).

One append-only row per independent hypothesis test evaluated anywhere in
the app — an AlphaCrafter grid config, an advisor challenger spawn, a
discover candidate cohort, a graduation portfolio variant. Every consumer of
Deflated Sharpe counts this table (via
``app.foundation.quant_metrics.resolve_n_trials``, the raw count since the
500 floor of ruling #9 was dropped on 2026-10-04) so the deflation reflects the TRUE global search
breadth instead of a per-context object count. Finding F15: the previous
per-site counts (portfolio rows, ``AdvisorStrategy`` rows, ...) ran 2-6,
making the deflation a rounding error; this ledger makes the gate un-gameable
— searching harder anywhere raises the bar everywhere.

Distinct from ``AcTrialLedger`` (``discover.py``): that table records
PER-FACTOR alpha-miner evaluations at ``(run_id, factor, symbol)`` grain for
one pipeline stage. This table is the cross-context trial COUNT the DSR
deflation itself consumes, written by every context that searches over
configurations/hypotheses (alphacrafter tuning is the first writer).
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.foundation.core.db_base import Base

from ._core import now_utc, uuid_pk


class TrialLedgerEntry(Base):
    """One recorded independent hypothesis test (an entry in ``n_trials``)."""

    __tablename__ = "trial_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    # Owning context, e.g. "alphacrafter_tuning", "advisor_challenger",
    # "discover_candidate", "graduation_variant" — free text, not an FK; a new
    # writer registers simply by calling record_trial with a new name.
    context: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Idempotency key scoped to `context` (e.g. "{job_run_id}:{config_hash}").
    # Mirrors AcTrialLedger's uq_ac_trial_append pattern: a retried job must
    # not double-count the same hypothesis test as two trials.
    trial_key: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # When the hypothesis was registered — condition 5 of the ADR 0015 gate
    # (out-of-sample confirmation on data timestamped after registration)
    # reads this, not created_at, once out-of-sample checking lands.
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True
    )

    __table_args__ = (
        sa.UniqueConstraint("context", "trial_key", name="uq_trial_ledger_context_key"),
    )
