"""Add alphacrafter_job_runs table for async pipeline progress tracking.

Revision ID: 0034_alphacrafter_job_run
Revises: 0033_regime_weights_factor_ic
Create Date: 2026-06-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_alphacrafter_job_run"
down_revision = "0033_regime_weights_factor_ic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("alphacrafter_job_runs"):
        op.create_table(
            "alphacrafter_job_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="running", index=True),
            sa.Column("progress_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("result_json", sa.Text, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("alphacrafter_job_runs"):
        op.drop_table("alphacrafter_job_runs")
