"""Add is_estimate and is_financial_advice columns to discovery_prediction

Every prediction output must carry is_estimate=True and
is_financial_advice=False per spec compliance.

Revision ID: 0066_add_prediction_flags
Revises: 0065_discovery_skill_snapshot
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0066_add_prediction_flags"
down_revision = "0065_discovery_skill_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("discovery_prediction"):
        columns = [c["name"] for c in inspector.get_columns("discovery_prediction")]
        if "is_estimate" not in columns:
            op.add_column(
                "discovery_prediction",
                sa.Column("is_estimate", sa.Boolean, nullable=False, server_default=sa.true()),
            )
        if "is_financial_advice" not in columns:
            op.add_column(
                "discovery_prediction",
                sa.Column("is_financial_advice", sa.Boolean, nullable=False, server_default=sa.false()),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("discovery_prediction"):
        columns = [c["name"] for c in inspector.get_columns("discovery_prediction")]
        if "is_financial_advice" in columns:
            op.drop_column("discovery_prediction", "is_financial_advice")
        if "is_estimate" in columns:
            op.drop_column("discovery_prediction", "is_estimate")
