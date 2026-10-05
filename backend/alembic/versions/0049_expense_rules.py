"""Add expense_rules table for automatic transaction categorization.

Revision ID: 0049_expense_rules
Revises: 0048_dkb_position_dedupe
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0049_expense_rules"
down_revision = "0048_dkb_position_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("expense_rules"):
        op.create_table(
            "expense_rules",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("pattern", sa.String(250), nullable=False),
            sa.Column("match_type", sa.String(20), nullable=False, server_default="contains"),
            sa.Column("category_id", sa.String(36), sa.ForeignKey("categories.id", ondelete="SET NULL"), nullable=True),
            sa.Column("priority", sa.Integer, nullable=False, server_default="10"),
            sa.Column("source", sa.String(20), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )


def downgrade() -> None:
    op.drop_table("expense_rules")
