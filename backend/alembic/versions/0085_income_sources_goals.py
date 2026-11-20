"""Student budget Phase 4 — income_sources + Category goal fields.

income_sources holds recurring expected income (projection-only, never
creates Expense rows). target_amount/target_date on categories mark a
category as a sinking-fund goal when target_amount is not None.

Revision ID: 0085_income_sources_goals
Revises: 0084_telegram_accounts
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0085_income_sources_goals"
down_revision = "0084_telegram_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("income_sources"):
        op.create_table(
            "income_sources",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("amount", sa.Numeric(20, 6), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("cadence", sa.String(20), nullable=False, server_default="monthly"),
            sa.Column("next_date", sa.Date(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )

    if inspector.has_table("categories"):
        cols = {c["name"] for c in inspector.get_columns("categories")}
        if "target_amount" not in cols:
            op.add_column("categories", sa.Column("target_amount", sa.Numeric(20, 6), nullable=True))
        if "target_date" not in cols:
            op.add_column("categories", sa.Column("target_date", sa.Date(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("categories"):
        cols = {c["name"] for c in inspector.get_columns("categories")}
        if "target_date" in cols:
            op.drop_column("categories", "target_date")
        if "target_amount" in cols:
            op.drop_column("categories", "target_amount")

    if inspector.has_table("income_sources"):
        op.drop_table("income_sources")
