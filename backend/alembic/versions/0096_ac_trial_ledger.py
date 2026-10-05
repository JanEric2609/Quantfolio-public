"""Add ac_trial_ledger table — signed IC trial persistence (M3a).

Audit F13 (docs/archive/audits/2026-08-discover-maths): the discover alpha-miner
stage aggregated abs(ic), masking anti-predictive factors. This additive
table stores SIGNED IC/ICIR trials keyed by
(run_id, factor_name, symbol, config_hash) — evaluated_at is deliberately
NOT part of uniqueness (re-evaluation within a run is a duplicate, not new
history). Oracle-corrected design; see
app/services/discover/trial_ledger.py.

Revision ID: 0096_ac_trial_ledger
Revises: 0095_scorecard_rps
"""

from alembic import op
import sqlalchemy as sa

revision = "0096_ac_trial_ledger"
down_revision = "0095_scorecard_rps"
branch_labels = None
depends_on = None


def _index_names(table_name):
    """Return the set of existing index names on ``table_name`` (empty if the table is gone)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def safe_create_index(index_name, table_name, columns, unique=False, **kwargs):
    """Create an index only if it does not already exist (idempotent / duplicate-safe)."""
    resolved = op.f(index_name)
    if resolved not in _index_names(table_name):
        op.create_index(resolved, table_name, columns, unique=unique, **kwargs)


def safe_drop_index(index_name, table_name=None, **kwargs):
    """Drop an index only if it exists (idempotent / safe on re-run)."""
    resolved = op.f(index_name)
    if table_name is not None and resolved in _index_names(table_name):
        op.drop_index(resolved, table_name=table_name, **kwargs)
    elif table_name is None:
        op.drop_index(resolved, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ac_trial_ledger"):
        op.create_table(
            "ac_trial_ledger",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("factor_name", sa.String(128), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            # SIGNED ic — negative = anti-predictive (F13).
            sa.Column("ic", sa.Float(), nullable=False),
            sa.Column("icir", sa.Float(), nullable=True),
            sa.Column("n_obs", sa.Integer(), nullable=False),
            sa.Column("horizon_days", sa.Integer(), nullable=False),
            sa.Column("config_hash", sa.String(64), nullable=False),
            sa.Column(
                "evaluated_at", sa.DateTime(timezone=True), nullable=False
            ),
        )
        safe_create_index(
            "uq_ac_trial_append",
            "ac_trial_ledger",
            ["run_id", "factor_name", "symbol", "config_hash"],
            unique=True,
        )
        safe_create_index(
            "ix_ac_trial_lookup",
            "ac_trial_ledger",
            ["factor_name", "config_hash", "evaluated_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ac_trial_ledger"):
        return
    indexes = {ix["name"] for ix in inspector.get_indexes("ac_trial_ledger")}
    if "ix_ac_trial_lookup" in indexes:
        safe_drop_index("ix_ac_trial_lookup", table_name="ac_trial_ledger")
    if "uq_ac_trial_append" in indexes:
        safe_drop_index("uq_ac_trial_append", table_name="ac_trial_ledger")
    op.drop_table("ac_trial_ledger")
