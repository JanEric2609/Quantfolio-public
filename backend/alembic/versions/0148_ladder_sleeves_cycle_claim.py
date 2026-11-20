"""Capital ladder, separate challenger sleeves, per-family trials, one cycle a day.

Plan 2026-11-10, ADR 0025. Additive only.

* ``llm_portfolio_decisions.cycle_date`` + partial unique index
  ``uq_llm_decision_cycle_slot``: one advisor cycle per sleeve per day (WS1).
  The backfill gives ``cycle_date`` only to the earliest slot-holding row per
  (portfolio, mandate, day) of advisor sleeves, so existing duplicates cannot
  violate the index. Mandate reviews (A/B) never get one.
* ``trial_ledger.config_hash`` / ``selection_set_id``: trials per family (WS2).
* ``discovery_prediction.strategy_id`` and ``advisor_shadow_forecasts``:
  per-strategy scoring and matched shadow forecasts (WS3).
* ``ladder_candidates`` / ``ladder_evidence_daily``: the capital ladder (WS4).

Revision ID: 0148_ladder_sleeves_cycle_claim
Revises: 0147_data_crosscheck_news
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

revision = "0148_ladder_sleeves_cycle_claim"
down_revision = "0147_data_crosscheck_news"
branch_labels = None
depends_on = None

_SLOT_STATUSES = ("running", "completed", "idle_no_candidates", "idle_no_trades")
_SLOT_PREDICATE = (
    "cycle_date IS NOT NULL AND status IN ("
    + ", ".join(f"'{s}'" for s in _SLOT_STATUSES)
    + ")"
)
_CYCLE_INDEX = "uq_llm_decision_cycle_slot"


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(i["name"]) for i in inspector.get_indexes(table) if i.get("name")}


def _is_advisor_mandate(mandate: str | None) -> bool:
    # The champion sleeve is "advisor", the shared challenger sleeve was
    # "advisor-challenger", and per-strategy sleeves are "adv-<id>".
    return bool(mandate) and (mandate == "advisor" or mandate.startswith("adv"))


def _backfill_cycle_date(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, portfolio_id, mandate, review_date, status "
            "FROM llm_portfolio_decisions WHERE cycle_date IS NULL"
        )
    ).fetchall()
    earliest: dict[tuple[str, str, object], tuple[object, str]] = {}
    for row_id, portfolio_id, mandate, review_date, status in rows:
        if not _is_advisor_mandate(mandate) or status not in _SLOT_STATUSES or review_date is None:
            continue
        key = (portfolio_id, mandate, _day_of(review_date))
        current = earliest.get(key)
        if current is None or str(review_date) < str(current[0]):
            earliest[key] = (review_date, row_id)
    by_day: dict[object, list[str]] = defaultdict(list)
    for (_pid, _mandate, day), (_stamp, row_id) in earliest.items():
        by_day[day].append(row_id)
    for day, ids in by_day.items():
        for row_id in ids:
            bind.execute(
                sa.text("UPDATE llm_portfolio_decisions SET cycle_date = :d WHERE id = :id"),
                {"d": day, "id": row_id},
            )


def _day_of(value: object) -> date:
    """The UTC day of a stored ``review_date`` (SQLite hands back text)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.date()
    return date.fromisoformat(str(value)[:10])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- WS1: one advisor cycle per sleeve per day -------------------------
    if inspector.has_table("llm_portfolio_decisions"):
        if "cycle_date" not in _columns(inspector, "llm_portfolio_decisions"):
            op.add_column(
                "llm_portfolio_decisions", sa.Column("cycle_date", sa.Date(), nullable=True)
            )
        _backfill_cycle_date(bind)
        if _CYCLE_INDEX not in _indexes(sa.inspect(bind), "llm_portfolio_decisions"):
            op.create_index(
                _CYCLE_INDEX,
                "llm_portfolio_decisions",
                ["portfolio_id", "mandate", "cycle_date"],
                unique=True,
                sqlite_where=sa.text(_SLOT_PREDICATE),
                postgresql_where=sa.text(_SLOT_PREDICATE),
            )

    # --- WS2: trials counted per family ------------------------------------
    if inspector.has_table("trial_ledger"):
        columns = _columns(inspector, "trial_ledger")
        if "config_hash" not in columns:
            op.add_column("trial_ledger", sa.Column("config_hash", sa.String(64), nullable=True))
        if "selection_set_id" not in columns:
            op.add_column(
                "trial_ledger", sa.Column("selection_set_id", sa.String(64), nullable=True)
            )

    # --- WS3: separate challenger sleeves and paired promotion -------------
    if inspector.has_table("discovery_prediction"):
        if "strategy_id" not in _columns(inspector, "discovery_prediction"):
            op.add_column(
                "discovery_prediction", sa.Column("strategy_id", sa.String(36), nullable=True)
            )
        if "ix_discovery_prediction_strategy_id" not in _indexes(sa.inspect(bind), "discovery_prediction"):
            op.create_index(
                "ix_discovery_prediction_strategy_id", "discovery_prediction", ["strategy_id"]
            )
    if not inspector.has_table("advisor_shadow_forecasts"):
        op.create_table(
            "advisor_shadow_forecasts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "strategy_id", sa.String(36),
                sa.ForeignKey("advisor_strategies.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("forecast_date", sa.Date(), nullable=False),
            sa.Column("ticker", sa.String(32), nullable=False),
            sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="21"),
            sa.Column("p_beat", sa.Float(), nullable=False),
            sa.Column("band_low", sa.Float(), nullable=False),
            sa.Column("band_high", sa.Float(), nullable=False),
            sa.Column("realised_return", sa.Float(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("beat", sa.Boolean(), nullable=True),
            sa.Column("brier", sa.Float(), nullable=True),
            sa.Column("rps", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("strategy_id", "forecast_date", "ticker", name="uq_shadow_forecast"),
        )
        op.create_index("ix_advisor_shadow_forecasts_user_id", "advisor_shadow_forecasts", ["user_id"])
        op.create_index("ix_advisor_shadow_forecasts_strategy_id", "advisor_shadow_forecasts", ["strategy_id"])
        op.create_index(
            "ix_advisor_shadow_forecasts_forecast_date", "advisor_shadow_forecasts", ["forecast_date"]
        )

    # --- WS4: the capital ladder -------------------------------------------
    if not inspector.has_table("ladder_candidates"):
        op.create_table(
            "ladder_candidates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("slot", sa.String(16), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("ref_id", sa.String(36), nullable=False),
            sa.Column("portfolio_id", sa.String(36), nullable=False),
            sa.Column("config_hash", sa.String(64), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("pi", sa.Float(), nullable=False),
            sa.Column("born_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("stage", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("stage_since", sa.DateTime(timezone=True), nullable=True),
            sa.Column("vetoed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "slot", "epoch", name="uq_ladder_candidate_epoch"),
        )
        op.create_index("ix_ladder_candidates_user_id", "ladder_candidates", ["user_id"])
        op.create_index("ix_ladder_candidates_portfolio_id", "ladder_candidates", ["portfolio_id"])
    if not inspector.has_table("ladder_evidence_daily"):
        op.create_table(
            "ladder_evidence_daily",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "candidate_id", sa.String(36),
                sa.ForeignKey("ladder_candidates.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("x", sa.Float(), nullable=True),
            sa.Column("sigma", sa.Float(), nullable=True),
            sa.Column("e_skill", sa.Float(), nullable=True),
            sa.Column("e_harm", sa.Float(), nullable=True),
            sa.Column("rel_drawdown", sa.Float(), nullable=True),
            sa.Column("te", sa.Float(), nullable=True),
            sa.Column("stage", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("candidate_id", "day", name="uq_ladder_evidence_day"),
        )
        op.create_index("ix_ladder_evidence_daily_candidate_id", "ladder_evidence_daily", ["candidate_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("ladder_evidence_daily"):
        op.drop_table("ladder_evidence_daily")
    if inspector.has_table("ladder_candidates"):
        op.drop_table("ladder_candidates")
    if inspector.has_table("advisor_shadow_forecasts"):
        op.drop_table("advisor_shadow_forecasts")
    if inspector.has_table("discovery_prediction"):
        if "ix_discovery_prediction_strategy_id" in _indexes(inspector, "discovery_prediction"):
            op.drop_index("ix_discovery_prediction_strategy_id", table_name="discovery_prediction")
        if "strategy_id" in _columns(inspector, "discovery_prediction"):
            with op.batch_alter_table("discovery_prediction") as batch:
                batch.drop_column("strategy_id")

    if inspector.has_table("trial_ledger"):
        columns = _columns(inspector, "trial_ledger")
        drop = [c for c in ("selection_set_id", "config_hash") if c in columns]
        if drop:
            with op.batch_alter_table("trial_ledger") as batch:
                for column in drop:
                    batch.drop_column(column)

    if inspector.has_table("llm_portfolio_decisions"):
        if _CYCLE_INDEX in _indexes(inspector, "llm_portfolio_decisions"):
            op.drop_index(_CYCLE_INDEX, table_name="llm_portfolio_decisions")
        if "cycle_date" in _columns(inspector, "llm_portfolio_decisions"):
            with op.batch_alter_table("llm_portfolio_decisions") as batch:
                batch.drop_column("cycle_date")
