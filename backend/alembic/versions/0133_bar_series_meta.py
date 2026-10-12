"""One pinned provider per stored bar series (plan 2026-10-12, D1).

Revision ID: 0133_bar_series_meta
Revises: 0132_monthly_plan_actions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0133_bar_series_meta"
down_revision = "0132_monthly_plan_actions"
branch_labels = None
depends_on = None

_TABLE = "bar_series_meta"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("symbol", sa.String(32), primary_key=True),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("adjusted", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_full_repull_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("quarantine_reason", sa.String(40), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
