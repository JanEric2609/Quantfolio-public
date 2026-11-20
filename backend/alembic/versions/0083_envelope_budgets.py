"""Envelope budgeting core (Money Phase 2) — envelope_budgets table.

One row per (user, category, year, month) storing the budgeted amount.
Rollover balance is derived at read time from history, never stored here.

Revision ID: 0083_envelope_budgets
Revises: 0082_student_budget_reset
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0083_envelope_budgets"
down_revision = "0082_student_budget_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("envelope_budgets"):
        op.create_table(
            "envelope_budgets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "category_id",
                sa.String(36),
                sa.ForeignKey("categories.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("month", sa.Integer(), nullable=False),
            sa.Column("budgeted_amount", sa.Numeric(20, 6), nullable=False),
            sa.UniqueConstraint(
                "user_id", "category_id", "year", "month", name="uq_envelope_budget_user_category_month"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("envelope_budgets"):
        op.drop_table("envelope_budgets")
