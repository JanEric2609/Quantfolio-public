"""The plan's one-year bands and their scores, append-only (plan 2026-10-14, D6).

Revision ID: 0140_plan_bands
Revises: 0139_risk_forecasts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0140_plan_bands"
down_revision = "0139_risk_forecasts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("plan_band_forecasts"):
        op.create_table(
            "plan_band_forecasts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("model", sa.String(32), nullable=False),
            sa.Column("model_version", sa.String(40), nullable=False),
            sa.Column("forecast_day", sa.Date(), nullable=False),
            sa.Column("end_day", sa.Date(), nullable=False),
            sa.Column("p10", sa.Float(), nullable=False),
            sa.Column("p50", sa.Float(), nullable=False),
            sa.Column("p90", sa.Float(), nullable=False),
            sa.Column("start_value", sa.Float(), nullable=False),
            sa.Column("monthly_contribution", sa.Float(), nullable=False),
            sa.Column("value_p10", sa.Float(), nullable=False),
            sa.Column("value_p50", sa.Float(), nullable=False),
            sa.Column("value_p90", sa.Float(), nullable=False),
            sa.Column("inputs_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "model", "forecast_day", name="uq_plan_band_user_model_day"),
        )
        op.create_index("ix_plan_band_forecasts_user_id", "plan_band_forecasts", ["user_id"])
    if not inspector.has_table("plan_band_scores"):
        op.create_table(
            "plan_band_scores",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("forecast_id", sa.String(36), sa.ForeignKey("plan_band_forecasts.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("start_day", sa.Date(), nullable=False),
            sa.Column("end_day", sa.Date(), nullable=False),
            sa.Column("nominal_return", sa.Float(), nullable=False),
            sa.Column("inflation", sa.Float(), nullable=False),
            sa.Column("real_return", sa.Float(), nullable=False),
            sa.Column("inside", sa.Boolean(), nullable=False),
            sa.Column("side", sa.Integer(), nullable=False),
            sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("forecast_id", name="uq_plan_band_scores_forecast_id"),
        )
        op.create_index("ix_plan_band_scores_forecast_id", "plan_band_scores", ["forecast_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("plan_band_scores"):
        op.drop_table("plan_band_scores")
    if inspector.has_table("plan_band_forecasts"):
        op.drop_table("plan_band_forecasts")
