"""discover_stage_cache: user-independent Discover stage outputs per symbol and bar date.

Plan 2026-11-10, WP9. Additive only.

Revision ID: 0152_discover_stage_cache
Revises: 0151_news_source_feed_ticker
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0152_discover_stage_cache"
down_revision = "0151_news_source_feed_ticker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("discover_stage_cache"):
        op.create_table(
            "discover_stage_cache",
            sa.Column("symbol", sa.String(32), primary_key=True),
            sa.Column("stage", sa.String(96), primary_key=True),
            sa.Column("bar_date", sa.String(10), primary_key=True),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("discover_stage_cache"):
        op.drop_table("discover_stage_cache")
