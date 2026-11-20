"""fundamentals_snapshots: dated copies of the cached fundamentals.

Plan 2026-11-11 (ML spec v2). Additive only.

Revision ID: 0153_fundamentals_snapshots
Revises: 0152_discover_stage_cache
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0153_fundamentals_snapshots"
down_revision = "0152_discover_stage_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("fundamentals_snapshots"):
        op.create_table(
            "fundamentals_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("data_json", sa.Text(), nullable=False),
            sa.Column("source", sa.String(40), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("symbol", "snapshot_date", name="uq_fundamentals_snapshot"),
        )
        op.create_index("ix_fundamentals_snapshots_symbol", "fundamentals_snapshots", ["symbol"])
        op.create_index("ix_fundamentals_snapshots_snapshot_date", "fundamentals_snapshots", ["snapshot_date"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("fundamentals_snapshots"):
        op.drop_table("fundamentals_snapshots")
