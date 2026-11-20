"""Make audit_logs.user_id nullable for system-level LLM events.

Revision ID: 0037_audit_log_user_nullable
Revises: 0036_research_report_horizon
Create Date: 2026-06-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_audit_log_user_nullable"
down_revision = "0036_research_report_horizon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("audit_logs"):
        with op.batch_alter_table("audit_logs") as batch_op:
            batch_op.alter_column(
                "user_id",
                existing_type=sa.String(length=36),
                nullable=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("audit_logs"):
        with op.batch_alter_table("audit_logs") as batch_op:
            batch_op.alter_column(
                "user_id",
                existing_type=sa.String(length=36),
                nullable=False,
            )
