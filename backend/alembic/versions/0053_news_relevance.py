"""Add relevance columns to news_items for portfolio-aware tagging.

Revision ID: 0053_news_relevance
Revises: 0052_purge_zero_dkb_expenses
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_news_relevance"
down_revision = "0052_purge_zero_dkb_expenses"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


def upgrade() -> None:
    if not _has_column("news_items", "relevance_score"):
        op.add_column(
            "news_items",
            sa.Column("relevance_score", sa.Numeric(4, 3), nullable=True),
        )
    if not _has_column("news_items", "relevance_label"):
        op.add_column(
            "news_items",
            sa.Column("relevance_label", sa.String(16), nullable=True),
        )
    if not _has_column("news_items", "relevance_reason"):
        op.add_column(
            "news_items",
            sa.Column("relevance_reason", sa.Text, nullable=True),
        )


def downgrade() -> None:
    if _has_column("news_items", "relevance_reason"):
        op.drop_column("news_items", "relevance_reason")
    if _has_column("news_items", "relevance_label"):
        op.drop_column("news_items", "relevance_label")
    if _has_column("news_items", "relevance_score"):
        op.drop_column("news_items", "relevance_score")
