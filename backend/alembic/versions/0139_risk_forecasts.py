"""Risk forecasts and their scores, append-only (plan 2026-10-14, D3).

Revision ID: 0139_risk_forecasts
Revises: 0138_paper_trade_asset
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0139_risk_forecasts"
down_revision = "0138_paper_trade_asset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("risk_forecasts"):
        op.create_table(
            "risk_forecasts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
            sa.Column("target", sa.String(80), nullable=False),
            sa.Column("label", sa.String(160), nullable=False),
            sa.Column("forecast_day", sa.Date(), nullable=False),
            sa.Column("horizon_days", sa.Integer(), nullable=False),
            sa.Column("model_version", sa.String(40), nullable=False),
            sa.Column("model_hash", sa.String(16), nullable=False),
            sa.Column("sigma", sa.Float(), nullable=False),
            sa.Column("var95", sa.Float(), nullable=False),
            sa.Column("var975", sa.Float(), nullable=False),
            sa.Column("es975", sa.Float(), nullable=False),
            sa.Column("n_obs", sa.Integer(), nullable=False),
            sa.Column("n_calibration", sa.Integer(), nullable=False),
            sa.Column("weights_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("target", "forecast_day", "horizon_days", name="uq_risk_forecast_target_day_h"),
        )
        op.create_index("ix_risk_forecasts_user_id", "risk_forecasts", ["user_id"])
        op.create_index("ix_risk_forecasts_target", "risk_forecasts", ["target"])
    if not inspector.has_table("risk_forecast_scores"):
        op.create_table(
            "risk_forecast_scores",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("forecast_id", sa.String(36), sa.ForeignKey("risk_forecasts.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("end_day", sa.Date(), nullable=False),
            sa.Column("realized_return", sa.Float(), nullable=False),
            sa.Column("loss", sa.Float(), nullable=False),
            sa.Column("var95_breach", sa.Boolean(), nullable=False),
            sa.Column("var_e", sa.Float(), nullable=False),
            sa.Column("es_e", sa.Float(), nullable=False),
            sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("forecast_id", name="uq_risk_forecast_scores_forecast_id"),
        )
        op.create_index("ix_risk_forecast_scores_forecast_id", "risk_forecast_scores", ["forecast_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("risk_forecast_scores"):
        op.drop_table("risk_forecast_scores")
    if inspector.has_table("risk_forecasts"):
        op.drop_table("risk_forecasts")
