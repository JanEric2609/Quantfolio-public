"""Add total_return_pct to portfolio_snapshots.

Revision ID: 0059_portfolio_snap_return_pct
Revises: 0058_purge_reddit_apikey
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_portfolio_snap_return_pct"
down_revision = "0058_purge_reddit_apikey"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("portfolio_snapshots"):
        cols = [c["name"] for c in inspector.get_columns("portfolio_snapshots")]
        if "total_return_pct" not in cols:
            op.add_column(
                "portfolio_snapshots",
                sa.Column("total_return_pct", sa.Numeric(8, 4), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("portfolio_snapshots"):
        cols = [c["name"] for c in inspector.get_columns("portfolio_snapshots")]
        if "total_return_pct" in cols:
            op.drop_column("portfolio_snapshots", "total_return_pct")
