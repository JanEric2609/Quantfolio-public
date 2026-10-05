"""Add user_news_relevance table for per-user news relevance scores.

Revision ID: 0056_user_news_relevance
Revises: 0055_llm_paper_mandates
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0056_user_news_relevance"
down_revision = "0055_llm_paper_mandates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("user_news_relevance"):
        op.create_table(
            "user_news_relevance",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("news_item_id", sa.String(36), sa.ForeignKey("news_items.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("relevance_score", sa.Float, nullable=True),
            sa.Column("relevance_label", sa.String(16), nullable=True),
            sa.Column("relevance_reason", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
            sa.UniqueConstraint("user_id", "news_item_id", name="uq_user_news_relevance"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("user_news_relevance"):
        op.drop_table("user_news_relevance")
