"""Add currency column to price_cache.

Revision ID: 0043_price_cache_curr
Revises: 0042_verification_rework
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_price_cache_curr"
down_revision = "0042_verification_rework"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("price_cache"):
        columns = {c["name"] for c in inspector.get_columns("price_cache")}
        if "currency" not in columns:
            op.add_column(
                "price_cache",
                sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("price_cache"):
        columns = {c["name"] for c in inspector.get_columns("price_cache")}
        if "currency" in columns:
            op.drop_column("price_cache", "currency")
