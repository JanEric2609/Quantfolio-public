"""learning_purchases: the learning sleeve's monthly ledger (ADR 0025 amendment 2026-11-12).

Additive only.

Revision ID: 0155_learning_purchases
Revises: 0154_fetch_misses
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0155_learning_purchases"
down_revision = "0154_fetch_misses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("learning_purchases"):
        op.create_table(
            "learning_purchases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("month", sa.Date(), nullable=False),
            sa.Column("isin", sa.String(12), nullable=False),
            sa.Column("ticker", sa.String(40), nullable=False),
            sa.Column("name", sa.String(200), nullable=True),
            sa.Column("amount_eur", sa.Float(), nullable=False),
            sa.Column("paper_price", sa.Float(), nullable=True),
            sa.Column("shadow_ticker", sa.String(40), nullable=False),
            sa.Column("shadow_price", sa.Float(), nullable=True),
            sa.Column("baseline_quantity", sa.Float(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(12), nullable=False, server_default="planned"),
            sa.Column("fill_quantity", sa.Float(), nullable=True),
            sa.Column("fill_price_eur", sa.Float(), nullable=True),
            sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "month", name="uq_learning_purchase_month"),
        )
        op.create_index("ix_learning_purchases_user_id", "learning_purchases", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("learning_purchases"):
        op.drop_index("ix_learning_purchases_user_id", table_name="learning_purchases")
        op.drop_table("learning_purchases")
