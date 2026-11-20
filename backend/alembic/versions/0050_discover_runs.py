"""Add discover_runs and discover_candidates tables for Phase 3 pipeline.

Revision ID: 0050_discover_runs
Revises: 0049_expense_rules
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_discover_runs"
down_revision = "0049_expense_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("discover_runs"):
        op.create_table(
            "discover_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="queued", index=True),
            sa.Column("stage_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("params_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not inspector.has_table("discover_candidates"):
        op.create_table(
            "discover_candidates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("discover_runs.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("symbol", sa.String(32), nullable=False, index=True),
            sa.Column("isin", sa.String(16), nullable=True),
            sa.Column("name", sa.String(180), nullable=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending", index=True),
            sa.Column("reject_stage", sa.String(32), nullable=True),
            sa.Column("reject_reason", sa.Text, nullable=True),
            sa.Column("scores_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("tradeable_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("dossier_id", sa.String(36), sa.ForeignKey("recommendation_dossiers.id", ondelete="SET NULL"), nullable=True),
            sa.Column("recommendation_id", sa.String(36), sa.ForeignKey("recommendations.id", ondelete="SET NULL"), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("discover_candidates")
    op.drop_table("discover_runs")
