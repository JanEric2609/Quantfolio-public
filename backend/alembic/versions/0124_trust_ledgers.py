"""Trust ledgers: drop confidence_scores, add regime_label_history.

The hand-weighted "confidence score" was never compared to any outcome, so the
Verification page is replaced by "Can I trust it?" (``/trust``), which scores
real, frozen predictions. ``confidence_scores`` goes with it.

``regime_label_history`` is one regime call per day. Existing
``regime_snapshots`` rows (the raw daily job output, written at issue time) are
carried over so the ledger starts with the history that already exists, the
last snapshot of each UTC day winning.

The Control Center settings that only fed the deleted score and its alerts
(benchmark symbol, snapshot look-back, underperformance, volatility and
confidence thresholds) are removed from ``app_settings``; the drawdown and
concentration thresholds stay.

Downgrade recreates ``confidence_scores`` empty (its rows are not recoverable)
with the columns and indexes from 0042/2ce7981ac5c5, and drops the new table.

Revision ID: 0124_trust_ledgers
Revises: 0123_broker_positions
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

revision = "0124_trust_ledgers"
down_revision = "0123_broker_positions"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_SNAPSHOT_COLUMNS = {"ts", "label", "source", "payload_json"}
_REMOVED_SETTINGS = (
    "verification_benchmark_symbol",
    "verification_max_snapshots_lookback",
    "verification_underperformance_threshold",
    "verification_var_breach_threshold",
    "verification_confidence_low",
    "verification_confidence_warning",
)


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _backfill_regime_history(bind: sa.engine.Connection, inspector: sa.Inspector) -> None:
    if not inspector.has_table("regime_snapshots"):
        return
    if not _SNAPSHOT_COLUMNS <= {c["name"] for c in inspector.get_columns("regime_snapshots")}:
        return
    rows = bind.execute(
        sa.text("SELECT ts, label, source, payload_json FROM regime_snapshots ORDER BY ts")
    ).fetchall()
    by_day: dict[date, dict[str, object]] = {}
    for ts, label, source, payload_json in rows:
        issued = _as_datetime(ts)
        if issued is None or not label:
            continue
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except (TypeError, ValueError):
            payload = {}
        probs = payload.get("probs") if isinstance(payload, dict) else None
        clean: dict[str, float] = {}
        if isinstance(probs, dict):
            for key, value in probs.items():
                try:
                    clean[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        by_day[issued.astimezone(UTC).date()] = {
            "id": str(uuid.uuid4()),
            "as_of": issued.astimezone(UTC).date(),
            "label": str(label),
            "probabilities_json": clean,
            "model": str(source or ""),
            "created_at": issued,
        }
    if not by_day:
        return
    table = sa.table(
        "regime_label_history",
        sa.column("id", sa.String),
        sa.column("as_of", sa.Date),
        sa.column("label", sa.String),
        sa.column("probabilities_json", sa.JSON),
        sa.column("model", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(table, [by_day[day] for day in sorted(by_day)])
    logger.info("0124: carried %d daily regime calls over from regime_snapshots", len(by_day))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("regime_label_history"):
        op.create_table(
            "regime_label_history",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("as_of", sa.Date(), nullable=False),
            sa.Column("label", sa.String(24), nullable=False),
            sa.Column("probabilities_json", sa.JSON(), nullable=False),
            sa.Column("model", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_regime_label_history_as_of", "regime_label_history", ["as_of"], unique=True
        )
        _backfill_regime_history(bind, inspector)

    if inspector.has_table("confidence_scores"):
        op.drop_table("confidence_scores")

    if inspector.has_table("app_settings"):
        for key in _REMOVED_SETTINGS:
            bind.execute(sa.text("DELETE FROM app_settings WHERE key = :key"), {"key": key})


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("confidence_scores"):
        op.create_table(
            "confidence_scores",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "portfolio_id",
                sa.String(),
                sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("overall", sa.Numeric(6, 4), nullable=False),
            sa.Column("historical_performance", sa.Numeric(6, 4), nullable=False),
            sa.Column("live_tracking", sa.Numeric(6, 4), nullable=False),
            sa.Column("risk_profile", sa.Numeric(6, 4), nullable=False),
            sa.Column("regime_adaptability", sa.Numeric(6, 4), nullable=False),
            sa.Column("factors_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_confidence_scores_portfolio_created",
            "confidence_scores",
            ["portfolio_id", "created_at"],
        )
        op.create_index(
            "ix_confidence_scores_portfolio_id", "confidence_scores", ["portfolio_id"]
        )

    if inspector.has_table("regime_label_history"):
        op.drop_table("regime_label_history")
