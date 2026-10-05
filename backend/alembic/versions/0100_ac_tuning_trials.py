"""AlphaCrafter tuning-trial ledger for the quarterly walk-forward job.

audit-fixes-2026-08 todo 20. One row per evaluated grid configuration per
tuning run (EVERY trial is logged, not only winners — AFML ch.12 doctrine).
Note the revision id: this migration was authored after 0101 in commit order
but chains from it because 0101 was already applied; alembic orders by
down_revision links, not numeric prefixes.

Revision ID: 0100_ac_tuning_trials
Revises: 0101_tax_ledger_dedupe
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0100_ac_tuning_trials"
down_revision = "0101_tax_ledger_dedupe"
branch_labels = None
depends_on = None

_TABLE = "alphacrafter_tuning_trials"


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
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "job_run_id",
                sa.String(length=36),
                sa.ForeignKey("alphacrafter_job_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("config_json", sa.JSON(), nullable=False),
            sa.Column("is_mean_ic", sa.Float(), nullable=False),
            sa.Column("oos_icir", sa.Float(), nullable=True),
            sa.Column("wfe", sa.Float(), nullable=True),
            sa.Column("dsr", sa.Float(), nullable=True),
            sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.func.now(),
            ),
        )
        safe_create_index("ix_alphacrafter_tuning_trials_job_run_id", _TABLE, ["job_run_id"])
        safe_create_index("ix_alphacrafter_tuning_trials_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
