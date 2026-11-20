"""Add target_price / alert fields to watchlist; add risk_profile to users.

Revision ID: 0035_watchlist_risk
Revises: 0034_alphacrafter_job_run
Create Date: 2026-06-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_watchlist_risk"
down_revision = "0034_alphacrafter_job_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- watchlist: add target_price, alert_triggered, alert_triggered_at ---
    if inspector.has_table("watchlist"):
        existing_cols = {c["name"] for c in inspector.get_columns("watchlist")}
        if "target_price" not in existing_cols:
            op.add_column(
                "watchlist",
                sa.Column("target_price", sa.Numeric(14, 4), nullable=True),
            )
        if "alert_triggered" not in existing_cols:
            op.add_column(
                "watchlist",
                sa.Column("alert_triggered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            )
        if "alert_triggered_at" not in existing_cols:
            op.add_column(
                "watchlist",
                sa.Column("alert_triggered_at", sa.DateTime(timezone=True), nullable=True),
            )

    # --- users: add risk_profile ---
    if inspector.has_table("users"):
        existing_cols = {c["name"] for c in inspector.get_columns("users")}
        if "risk_profile" not in existing_cols:
            op.add_column(
                "users",
                sa.Column("risk_profile", sa.String(16), server_default="moderate", nullable=False),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("users"):
        existing_cols = {c["name"] for c in inspector.get_columns("users")}
        if "risk_profile" in existing_cols:
            op.drop_column("users", "risk_profile")

    if inspector.has_table("watchlist"):
        existing_cols = {c["name"] for c in inspector.get_columns("watchlist")}
        if "alert_triggered_at" in existing_cols:
            op.drop_column("watchlist", "alert_triggered_at")
        if "alert_triggered" in existing_cols:
            op.drop_column("watchlist", "alert_triggered")
        if "target_price" in existing_cols:
            op.drop_column("watchlist", "target_price")
