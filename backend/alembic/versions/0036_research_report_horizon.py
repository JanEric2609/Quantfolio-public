"""Add stock_research_reports and multi_horizon_verdicts tables.

Revision ID: 0036_research_report_horizon
Revises: 0035_watchlist_risk
Create Date: 2026-06-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_research_report_horizon"
down_revision = "0035_watchlist_risk"
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

    if not inspector.has_table("stock_research_reports"):
        op.create_table(
            "stock_research_reports",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ticker", sa.String(32), nullable=False),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("report_json", sa.Text(), nullable=False),
            sa.Column("executive_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("data_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        safe_create_index("ix_stock_research_reports_ticker", "stock_research_reports", ["ticker"])
        safe_create_index("ix_stock_research_reports_ticker_user", "stock_research_reports", ["ticker", "user_id"])

    if not inspector.has_table("multi_horizon_verdicts"):
        op.create_table(
            "multi_horizon_verdicts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ticker", sa.String(32), nullable=False),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("horizons_json", sa.Text(), nullable=False, server_default="[]"),
        )
        safe_create_index("ix_multi_horizon_verdicts_ticker", "multi_horizon_verdicts", ["ticker"])
        safe_create_index("ix_multi_horizon_verdicts_ticker_user", "multi_horizon_verdicts", ["ticker", "user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("multi_horizon_verdicts"):
        safe_drop_index("ix_multi_horizon_verdicts_ticker_user", "multi_horizon_verdicts")
        safe_drop_index("ix_multi_horizon_verdicts_ticker", "multi_horizon_verdicts")
        op.drop_table("multi_horizon_verdicts")
    if inspector.has_table("stock_research_reports"):
        safe_drop_index("ix_stock_research_reports_ticker_user", "stock_research_reports")
        safe_drop_index("ix_stock_research_reports_ticker", "stock_research_reports")
        op.drop_table("stock_research_reports")
