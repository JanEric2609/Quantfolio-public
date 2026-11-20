"""Purge legacy zero-amount DKB rows created by the broken mt940 field parsing.

Before the adapter read mt940 fields from ``statement.data``, every synced
transaction arrived with amount 0, an empty reference, and the sync-day date.
Those rows are pure noise (one identical EUR 0.00 row per sync-day) and block
re-import of the real transactions because their dedupe hashes collide with
nothing meaningful. Data-only, idempotent.

Revision ID: 0052_purge_zero_dkb_expenses
Revises: 0051_dkb_constraints
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0052_purge_zero_dkb_expenses"
down_revision = "0051_dkb_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("expenses"):
        op.execute("DELETE FROM expenses WHERE source = 'dkb_auto' AND amount = 0")

    if inspector.has_table("dkb_transactions"):
        op.execute("DELETE FROM dkb_transactions WHERE source = 'dkb' AND amount = 0")

    if inspector.has_table("activity_ledger_entries"):
        op.execute(
            "DELETE FROM activity_ledger_entries WHERE source = 'dkb' AND amount = 0"
        )


def downgrade() -> None:
    pass  # data-only purge of corrupt rows; nothing to restore
