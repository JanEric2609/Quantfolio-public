"""analyst_estimate_snapshots: daily consensus EPS per symbol.

Discover's estimate-revision signal read a WRDS IBES extract that nobody
refreshes; past its 92-day staleness limit the signal is empty. A nightly
job now stores Yahoo's EPS trend (current fiscal year: today and 7/30/60/90
days ago) for the stocks Discover evaluates, and the signal falls back to
its 30-day revision when IBES has nothing current.

Revision ID: 0119_analyst_estimate_snapshots
Revises: 0118_index_currency_weights
"""
from alembic import op
import sqlalchemy as sa

revision = "0119_analyst_estimate_snapshots"
down_revision = "0118_index_currency_weights"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("analyst_estimate_snapshots"):
        op.create_table(
            "analyst_estimate_snapshots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("period", sa.String(length=8), nullable=False),
            sa.Column("eps_current", sa.Float(), nullable=True),
            sa.Column("eps_7d_ago", sa.Float(), nullable=True),
            sa.Column("eps_30d_ago", sa.Float(), nullable=True),
            sa.Column("eps_60d_ago", sa.Float(), nullable=True),
            sa.Column("eps_90d_ago", sa.Float(), nullable=True),
            sa.Column("analysts", sa.Integer(), nullable=True),
            sa.Column("currency", sa.String(length=3), nullable=True),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("symbol", "snapshot_date", "period", name="uq_analyst_estimate_snapshot"),
        )
        op.create_index("ix_analyst_estimate_snapshots_symbol", "analyst_estimate_snapshots", ["symbol"])
        op.create_index("ix_analyst_estimate_snapshots_snapshot_date", "analyst_estimate_snapshots", ["snapshot_date"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("analyst_estimate_snapshots"):
        op.drop_index("ix_analyst_estimate_snapshots_snapshot_date", table_name="analyst_estimate_snapshots")
        op.drop_index("ix_analyst_estimate_snapshots_symbol", table_name="analyst_estimate_snapshots")
        op.drop_table("analyst_estimate_snapshots")
