"""Add updated_at to DkbAccount/DkbPosition.

Revision ID: 0030_dkb_schema_repairs
Revises: 0029_dkb_diagnostic_debug_log
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa


revision = "0030_dkb_schema_repairs"
down_revision = "0029_dkb_diagnostic_debug_log"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


def upgrade() -> None:
    # 1. Add updated_at to DkbAccount
    if not _has_column("dkb_accounts", "updated_at"):
        op.add_column(
            "dkb_accounts",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
        )

    # 2. Add updated_at to DkbPosition
    if not _has_column("dkb_positions", "updated_at"):
        op.add_column(
            "dkb_positions",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    if _has_column("dkb_accounts", "updated_at"):
        op.drop_column("dkb_accounts", "updated_at")
    if _has_column("dkb_positions", "updated_at"):
        op.drop_column("dkb_positions", "updated_at")
