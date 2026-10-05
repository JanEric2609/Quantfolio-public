from sqlalchemy.orm import Mapped, mapped_column
from app.foundation.core.db_base import Base
from ._core import now_utc, uuid_pk
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text


class QValue(Base):
    """Dual-Q learning state-action values (Phase 5)."""

    __tablename__ = "q_values"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state_hash: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    q_theta: Mapped[float] = mapped_column(Float)  # learning network
    q_phi: Mapped[float] = mapped_column(Float)  # execution network
    visited_count: Mapped[int] = mapped_column(Integer, default=1)
    reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_state_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    regime_label: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    crisis: Mapped[bool] = mapped_column(Boolean, default=False)
    state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss: Mapped[float | None] = mapped_column(Float, nullable=True)


class MetaPolicySnapshot(Base):
    """MAML meta-policy snapshot (Phase 5). Regime-specific mixture weights."""

    __tablename__ = "meta_policy_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_pk)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    regime_label: Mapped[str] = mapped_column(String(24), index=True)
    weights_json: Mapped[str] = mapped_column(Text)  # {"bull": 0.4, "bear": 0.3, "sideways": 0.3}
    support_set_meta_json: Mapped[str] = mapped_column(
        Text
    )  # {"start_ts": ..., "episodes": N, "validation_sharpe": 0.8}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
