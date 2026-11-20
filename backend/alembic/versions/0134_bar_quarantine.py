"""Bars the ingest guard kept out of bar_prices (plan 2026-10-12, D2).

Revision ID: 0134_bar_quarantine
Revises: 0133_bar_series_meta
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0134_bar_quarantine"
down_revision = "0133_bar_series_meta"
branch_labels = None
depends_on = None

_TABLE = "bar_quarantine"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=False), nullable=False),
            sa.Column("open", sa.Float(), nullable=True),
            sa.Column("high", sa.Float(), nullable=True),
            sa.Column("low", sa.Float(), nullable=True),
            sa.Column("close", sa.Float(), nullable=False),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("reason", sa.String(40), nullable=False),
            sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolution", sa.String(80), nullable=True),
            sa.UniqueConstraint("symbol", "ts", "provider", name="uq_bar_quarantine_symbol_ts_provider"),
        )
        op.create_index("ix_bar_quarantine_symbol", _TABLE, ["symbol"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.drop_index("ix_bar_quarantine_symbol", table_name=_TABLE)
    op.drop_table(_TABLE)
