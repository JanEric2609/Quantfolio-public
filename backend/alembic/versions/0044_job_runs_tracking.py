"""Add job_runs table for scheduled job execution tracking.

Revision ID: 0044_job_runs_tracking
Revises: 0043_price_cache_curr
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0044_job_runs_tracking"
down_revision = "0043_price_cache_curr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("job_runs"):
        op.create_table(
            "job_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("job_name", sa.String(128), nullable=False, index=True),
            sa.Column("status", sa.String(24), nullable=False, server_default=sa.text("'running'"), index=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("triggered_by", sa.String(64), nullable=False, server_default=sa.text("'scheduler'")),
        )


def downgrade() -> None:
    op.drop_table("job_runs")
