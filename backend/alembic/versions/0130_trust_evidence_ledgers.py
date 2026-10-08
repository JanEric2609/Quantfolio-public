"""Trust evidence ledgers and the append-only guard (ADR 0018).

* ``discovery_prediction.provenance_json``: what produced each call.
* ``discover_candidate_snapshot``, ``candidate_outcome``,
  ``trust_daily_active_returns``: the shadow ledger and the calendar-time
  series the pre-registered tests bet on.
* Postgres only: BEFORE UPDATE triggers. The three new tables reject every
  UPDATE; ``discovery_prediction`` rejects a change to any issue-time column,
  lets ``conviction_calibrated`` and ``provenance_json`` go from NULL to a
  value only, and leaves the outcome columns writable. Deletes stay possible
  (user deletion cascades). A deliberate data repair must drop the trigger in
  its own migration and say why.

SQLite (tests, the migration dry-run) gets the tables and the column but no
triggers.

Revision ID: 0130_trust_evidence_ledgers
Revises: 0129_paper_benchmark_base
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0130_trust_evidence_ledgers"
down_revision = "0129_paper_benchmark_base"
branch_labels = None
depends_on = None

_PREDICTIONS = "discovery_prediction"
_APPEND_ONLY = ("discover_candidate_snapshot", "candidate_outcome", "trust_daily_active_returns")

# Issue-time columns of discovery_prediction: fixed when the call is made.
_FROZEN_SCALARS = (
    "user_id", "run_id", "asset_id", "symbol", "isin", "predicted_at", "horizon_days",
    "resolve_at", "direction", "conviction", "expected_return", "expected_return_low",
    "expected_return_high", "thesis", "price_at_prediction", "config_id", "portfolio_id",
    "is_estimate", "is_financial_advice",
)
_FROZEN_JSON = ("features_json",)
# May be set once (NULL -> value) and never changed after.
_SET_ONCE = ("conviction_calibrated",)
_SET_ONCE_JSON = ("provenance_json",)


def _prediction_guard_sql() -> str:
    changed = [f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _FROZEN_SCALARS]
    changed += [f"NEW.{c}::jsonb IS DISTINCT FROM OLD.{c}::jsonb" for c in _FROZEN_JSON]
    changed += [f"(OLD.{c} IS NOT NULL AND NEW.{c} IS DISTINCT FROM OLD.{c})" for c in _SET_ONCE]
    # A JSON 'null' counts as unset, like SQL NULL.
    changed += [
        f"(OLD.{c} IS NOT NULL AND OLD.{c}::jsonb <> 'null'::jsonb"
        f" AND NEW.{c}::jsonb IS DISTINCT FROM OLD.{c}::jsonb)"
        for c in _SET_ONCE_JSON
    ]
    condition = "\n     OR ".join(changed)
    return f"""
CREATE OR REPLACE FUNCTION trust_freeze_prediction_issue() RETURNS trigger AS $$
BEGIN
  IF {condition} THEN
    RAISE EXCEPTION 'discovery_prediction %: issue-time columns are append-only (ADR 0018)', OLD.id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


_REJECT_UPDATE_SQL = """
CREATE OR REPLACE FUNCTION trust_reject_update() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '%: rows are append-only (ADR 0018)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""


def _create_tables(inspector) -> None:
    if not inspector.has_table("discover_candidate_snapshot"):
        op.create_table(
            "discover_candidate_snapshot",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("issue_date", sa.Date(), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("isin", sa.String(16), nullable=True),
            sa.Column("source", sa.String(32), nullable=True),
            sa.Column("instrument_group", sa.String(8), nullable=False),
            sa.Column("sector", sa.String(64), nullable=True),
            sa.Column("evaluable", sa.Boolean(), nullable=False),
            sa.Column("reject_stage", sa.String(32), nullable=True),
            sa.Column("reject_reason", sa.Text(), nullable=True),
            sa.Column("composite", sa.Float(), nullable=True),
            sa.Column("composite_raw", sa.Float(), nullable=True),
            sa.Column("components_json", sa.JSON(), nullable=False),
            sa.Column("stock_rank", sa.Integer(), nullable=True),
            sa.Column("n_evaluable_stocks", sa.Integer(), nullable=False),
            sa.Column("shortlisted", sa.Boolean(), nullable=False),
            sa.Column("sector_capped", sa.Boolean(), nullable=False),
            sa.Column("picked", sa.Boolean(), nullable=False),
            sa.Column("entry_close", sa.Float(), nullable=True),
            sa.Column("entry_close_date", sa.Date(), nullable=True),
            sa.Column("cohort_id", sa.String(64), nullable=False),
            sa.Column("provenance_json", sa.JSON(), nullable=False),
            sa.Column("backfilled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "symbol", name="uq_candidate_snapshot_run_symbol"),
        )
        for col in ("user_id", "run_id", "issue_date", "symbol", "cohort_id"):
            op.create_index(f"ix_discover_candidate_snapshot_{col}", "discover_candidate_snapshot", [col])

    if not inspector.has_table("candidate_outcome"):
        op.create_table(
            "candidate_outcome",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "snapshot_id", sa.String(36),
                sa.ForeignKey("discover_candidate_snapshot.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("horizon_days", sa.Integer(), nullable=False),
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("entry_date", sa.Date(), nullable=True),
            sa.Column("exit_date", sa.Date(), nullable=True),
            sa.Column("ret_eur", sa.Float(), nullable=True),
            sa.Column("bench_ret_eur", sa.Float(), nullable=True),
            sa.Column("excess_eur", sa.Float(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("benchmark", sa.String(32), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("snapshot_id", "horizon_days", name="uq_candidate_outcome_horizon"),
        )
        for col in ("snapshot_id", "user_id", "run_id"):
            op.create_index(f"ix_candidate_outcome_{col}", "candidate_outcome", [col])

    if not inspector.has_table("trust_daily_active_returns"):
        op.create_table(
            "trust_daily_active_returns",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("series", sa.String(16), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("n_open", sa.Integer(), nullable=False),
            sa.Column("n_stale", sa.Integer(), nullable=False),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("benchmark", sa.String(32), nullable=False),
            sa.Column("detail_json", sa.JSON(), nullable=False),
            sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "series", "day", name="uq_trust_daily_series_day"),
        )
        op.create_index("ix_trust_daily_active_returns_user_id", "trust_daily_active_returns", ["user_id"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table(_PREDICTIONS):
        columns = {c["name"] for c in inspector.get_columns(_PREDICTIONS)}
        if "provenance_json" not in columns:
            op.add_column(_PREDICTIONS, sa.Column("provenance_json", sa.JSON(), nullable=True))

    _create_tables(inspector)

    if bind.dialect.name != "postgresql":
        return
    # get_bind() is already inside env.py's transaction: execute directly.
    bind.execute(sa.text(_REJECT_UPDATE_SQL))
    for table in _APPEND_ONLY:
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"))
        bind.execute(sa.text(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION trust_reject_update()"
        ))
    if inspector.has_table(_PREDICTIONS):
        bind.execute(sa.text(_prediction_guard_sql()))
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_PREDICTIONS}_freeze_issue ON {_PREDICTIONS}"))
        bind.execute(sa.text(
            f"CREATE TRIGGER trg_{_PREDICTIONS}_freeze_issue BEFORE UPDATE ON {_PREDICTIONS} "
            "FOR EACH ROW EXECUTE FUNCTION trust_freeze_prediction_issue()"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if bind.dialect.name == "postgresql":
        if inspector.has_table(_PREDICTIONS):
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_PREDICTIONS}_freeze_issue ON {_PREDICTIONS}"))
        bind.execute(sa.text("DROP FUNCTION IF EXISTS trust_freeze_prediction_issue()"))
        for table in _APPEND_ONLY:
            if inspector.has_table(table):
                bind.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"))
        bind.execute(sa.text("DROP FUNCTION IF EXISTS trust_reject_update()"))

    for table in ("trust_daily_active_returns", "candidate_outcome", "discover_candidate_snapshot"):
        if inspector.has_table(table):
            op.drop_table(table)

    if inspector.has_table(_PREDICTIONS):
        columns = {c["name"] for c in inspector.get_columns(_PREDICTIONS)}
        if "provenance_json" in columns:
            with op.batch_alter_table(_PREDICTIONS) as batch:
                batch.drop_column("provenance_json")
