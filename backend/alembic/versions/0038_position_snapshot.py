"""Add position_snapshots table for daily DKB holding snapshots.

Revision ID: 0038_position_snapshot
Revises: 0037_audit_log_user_nullable
Create Date: 2026-06-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_position_snapshot"
down_revision = "0037_audit_log_user_nullable"
branch_labels = None
depends_on = None


def _index_names(table_name):
    """Return the set of existing index names on ``table_name`` (empty if the table is gone)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def safe_create_index(index_name, table_name, columns, unique=False, **kwargs):
    """Create an index only if it does not already exist (idempotent / duplicate-safe)."""
    resolved = op.f(index_name)
    if resolved not in _index_names(table_name):
        op.create_index(resolved, table_name, columns, unique=unique, **kwargs)


def safe_drop_index(index_name, table_name=None, **kwargs):
    """Drop an index only if it exists (idempotent / safe on re-run)."""
    resolved = op.f(index_name)
    if table_name is not None and resolved in _index_names(table_name):
        op.drop_index(resolved, table_name=table_name, **kwargs)
    elif table_name is None:
        op.drop_index(resolved, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("position_snapshots"):
        op.create_table(
            "position_snapshots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "account_id",
                sa.String(length=36),
                sa.ForeignKey("dkb_accounts.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("snapshot_date", sa.Date(), nullable=False, index=True),
            sa.Column("isin", sa.String(length=12), nullable=False),
            sa.Column("ticker", sa.String(length=32), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("avg_buy_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("current_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("current_value", sa.Numeric(20, 6), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        safe_create_index(
            "ix_position_snapshots_account_date",
            "position_snapshots",
            ["account_id", "snapshot_date"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("position_snapshots"):
        safe_drop_index("ix_position_snapshots_account_date", "position_snapshots")
        op.drop_table("position_snapshots")
