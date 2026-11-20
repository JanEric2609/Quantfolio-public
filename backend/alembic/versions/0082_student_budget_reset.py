"""Wipe dummy/test Expense and Category rows ahead of the student budget category reset.

Confirmed against the live Postgres instance (2026-07-24, project owner) that
neither table holds real financial history — the Money module's data was
never used in earnest. Category re-seeding happens lazily via the existing
``ensure_default_categories`` the next time ``GET /api/budget/categories``
runs, same as it does today. Data-only, idempotent.

Revision ID: 0082_student_budget_reset
Revises: 0081_advisor_evolution
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0082_student_budget_reset"
down_revision = "0081_advisor_evolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("expenses"):
        op.execute("DELETE FROM expenses")

    if inspector.has_table("categories"):
        op.execute("DELETE FROM categories")


def downgrade() -> None:
    pass  # data-only wipe of dummy/test rows; nothing to restore
