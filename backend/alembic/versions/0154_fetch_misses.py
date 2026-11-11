"""fetch_misses: symbols the providers could not serve, kept across restarts.

Plan 2026-11-11. Additive only.

Revision ID: 0154_fetch_misses
Revises: 0153_fundamentals_snapshots
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0154_fetch_misses"
down_revision = "0153_fundamentals_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("fetch_misses"):
        op.create_table(
            "fetch_misses",
            sa.Column("symbol", sa.String(32), primary_key=True),
            sa.Column("misses", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_miss", sa.DateTime(timezone=True), nullable=False),
            sa.Column("message", sa.String(200), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("fetch_misses"):
        op.drop_table("fetch_misses")
