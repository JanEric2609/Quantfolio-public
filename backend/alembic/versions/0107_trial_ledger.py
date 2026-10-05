"""Add trial_ledger table — global experiment count for DSR deflation (ADR 0015 Phase 1).

Finding F15: n_trials call sites counted local objects (portfolio rows,
AdvisorStrategy rows) rather than actual search breadth, typically running
2-6 and making the Deflated Sharpe a rounding error. This table is the
cross-context ledger `quant_metrics.resolve_n_trials` counts (floored at
500, ruling #9) so every DSR call site deflates against the true global
trial count. See app/models/entities/trial_ledger.py.

Revision ID: 0107_trial_ledger
Revises: 0106_restore_hypertable_writes
"""

from alembic import op
import sqlalchemy as sa

revision = "0107_trial_ledger"
down_revision = "0106_restore_hypertable_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("trial_ledger"):
        op.create_table(
            "trial_ledger",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("context", sa.String(64), nullable=False),
            sa.Column("trial_key", sa.String(128), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_trial_ledger_context", "trial_ledger", ["context"],
        )
        op.create_index(
            "ix_trial_ledger_created_at", "trial_ledger", ["created_at"],
        )
        op.create_index(
            "uq_trial_ledger_context_key",
            "trial_ledger",
            ["context", "trial_key"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("trial_ledger"):
        return
    op.drop_table("trial_ledger")
