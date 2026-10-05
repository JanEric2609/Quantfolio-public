"""Add recommendation_reports and recommendation_items tables.

Revision ID: 0040_recommendation_engine
Revises: 0039_goal_extended
Create Date: 2026-06-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_recommendation_engine"
down_revision = "0039_goal_extended"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("recommendation_reports"):
        # For JSON columns, use dialect-agnostic defaults
        weights_default = sa.text("'{}'::jsonb") if bind.dialect.name == "postgresql" else "{}"
        source_health_default = sa.text("'{}'::jsonb") if bind.dialect.name == "postgresql" else "{}"
        stripped_claims_default = sa.text("'[]'::jsonb") if bind.dialect.name == "postgresql" else "[]"
        
        op.create_table(
            "recommendation_reports",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("portfolio_value", sa.Float, server_default="0.0"),
            sa.Column("currency", sa.String(3), server_default="EUR"),
            sa.Column("regime_label", sa.String(32), server_default="unknown"),
            sa.Column("regime_confidence", sa.Float, server_default="0.0"),
            sa.Column("estimate", sa.Boolean, server_default=sa.text("true")),
            sa.Column("not_tax_advice", sa.Boolean, server_default=sa.text("true")),
            sa.Column("weights_json", sa.JSON, server_default=weights_default),
            sa.Column("source_health_json", sa.JSON, server_default=source_health_default),
            sa.Column("raw_llm_output", sa.Text, server_default=""),
            sa.Column("validated_output", sa.Text, server_default=""),
            sa.Column("stripped_claims_json", sa.JSON, server_default=stripped_claims_default),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not inspector.has_table("recommendation_items"):
        # For JSON columns, use dialect-agnostic defaults
        evidence_default = sa.text("'[]'::jsonb") if bind.dialect.name == "postgresql" else "[]"
        risks_default = sa.text("'[]'::jsonb") if bind.dialect.name == "postgresql" else "[]"
        
        op.create_table(
            "recommendation_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("report_id", sa.String(36), sa.ForeignKey("recommendation_reports.id", ondelete="CASCADE"), index=True),
            sa.Column("ticker", sa.String(32), nullable=False, index=True),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("confidence", sa.String(16), server_default="medium"),
            sa.Column("timeframe", sa.String(64), server_default="3-6 months"),
            sa.Column("thesis", sa.Text, server_default=""),
            sa.Column("evidence_json", sa.JSON, server_default=evidence_default),
            sa.Column("risks_json", sa.JSON, server_default=risks_default),
            sa.Column("data_quality", sa.String(16), server_default="complete"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("recommendation_items"):
        op.drop_table("recommendation_items")

    if inspector.has_table("recommendation_reports"):
        op.drop_table("recommendation_reports")
