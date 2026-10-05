"""reports and recommendation context

Revision ID: 0002_reports_context
Revises: 0001_initial_schema
Create Date: 2026-05-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_reports_context"
down_revision = "0001_initial_schema"
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
    if not inspector.has_table("analysis_reports"):
        op.create_table(
            "analysis_reports",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ticker", sa.String(length=32), nullable=True),
            sa.Column("horizon", sa.String(length=24), nullable=False, server_default="mid"),
            sa.Column("title", sa.String(length=180), nullable=False, server_default="TradingAgents report"),
            sa.Column("source", sa.String(length=40), nullable=False, server_default="manual"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_analysis_reports_user_id", "analysis_reports", ["user_id"])
        safe_create_index("ix_analysis_reports_ticker", "analysis_reports", ["ticker"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("analysis_reports"):
        safe_drop_index("ix_analysis_reports_ticker", table_name="analysis_reports")
        safe_drop_index("ix_analysis_reports_user_id", table_name="analysis_reports")
        op.drop_table("analysis_reports")
