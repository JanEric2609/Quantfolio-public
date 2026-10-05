"""Add debug_log column to dkb_diagnostic_runs.

Revision ID: 0029_dkb_diagnostic_debug_log
Revises: 0028_password_reset_tokens
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa


revision = "0029_dkb_diagnostic_debug_log"
down_revision = "0028_password_reset_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("dkb_diagnostic_runs"):
        columns = [c["name"] for c in inspector.get_columns("dkb_diagnostic_runs")]
        if "debug_log" not in columns:
            op.add_column(
                "dkb_diagnostic_runs",
                sa.Column("debug_log", sa.Text, nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("dkb_diagnostic_runs"):
        columns = [c["name"] for c in inspector.get_columns("dkb_diagnostic_runs")]
        if "debug_log" in columns:
            op.drop_column("dkb_diagnostic_runs", "debug_log")
