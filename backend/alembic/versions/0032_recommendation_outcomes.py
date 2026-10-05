"""Add recommendation_outcomes table for portfolio advisor verification loop.

Revision ID: 0032_recommendation_outcomes
Revises: 0031_add_dkb_position_ticker
Create Date: 2026-06-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_recommendation_outcomes"
down_revision = "0031_add_dkb_position_ticker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("recommendation_outcomes"):
        op.create_table(
            "recommendation_outcomes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "recommendation_id", sa.String(36),
                sa.ForeignKey("recommendations.id", ondelete="CASCADE"),
                nullable=False, unique=True, index=True,
            ),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_days", sa.Integer, nullable=False, server_default="60"),
            sa.Column("abs_return", sa.Float),
            sa.Column("benchmark_excess_return", sa.Float),
            sa.Column("portfolio_sortino_delta", sa.Float),
            sa.Column("outcome_label", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("notes_json", sa.Text),
        )


def downgrade() -> None:
    op.drop_table("recommendation_outcomes")
