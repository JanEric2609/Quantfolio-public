"""Make holdings.avg_buy_price nullable — DKB sync must be able to record
'cost basis unknown' when FinTS omits acquisitionprice, instead of silently
storing zero (which then poisons attribution/risk/confidence calculations
that treat avg_buy_price as a value proxy).

Revision ID: 0086_holdings_avg_buy_nullable
Revises: 0085_income_sources_goals
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0086_holdings_avg_buy_nullable"
down_revision = "0085_income_sources_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("holdings"):
        return
    col = next((c for c in inspector.get_columns("holdings") if c["name"] == "avg_buy_price"), None)
    if col is None or col["nullable"]:
        return
    with op.batch_alter_table("holdings") as batch_op:
        batch_op.alter_column(
            "avg_buy_price",
            existing_type=sa.Numeric(20, 6),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("holdings"):
        return
    col = next((c for c in inspector.get_columns("holdings") if c["name"] == "avg_buy_price"), None)
    if col is None or not col["nullable"]:
        return
    # Backfill any NULLs to 0 before re-adding NOT NULL, or the ALTER will fail.
    op.execute("UPDATE holdings SET avg_buy_price = 0 WHERE avg_buy_price IS NULL")
    with op.batch_alter_table("holdings") as batch_op:
        batch_op.alter_column(
            "avg_buy_price",
            existing_type=sa.Numeric(20, 6),
            nullable=False,
        )
