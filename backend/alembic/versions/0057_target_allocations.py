"""Add target_allocations table for portfolio drift targets.

Revision ID: 0057_target_allocations
Revises: 0056_user_news_relevance
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0057_target_allocations"
down_revision = "0056_user_news_relevance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("target_allocations"):
        op.create_table(
            "target_allocations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("asset_type", sa.String(32), nullable=False),
            sa.Column("target_pct", sa.Float, nullable=False, server_default=sa.text("0.0")),
            sa.Column("tolerance_pct", sa.Float, nullable=False, server_default=sa.text("5.0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "asset_type", name="uq_target_allocations_user_asset"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("target_allocations"):
        op.drop_table("target_allocations")
