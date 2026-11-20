"""Paper portfolios: an inception date, dividends as cash flows, an archive for resets.

A reset restarts a paper portfolio from a clean date. Its holdings, trades,
snapshots and NAV-derived scorecards move into paper_portfolio_archives as one
JSON payload instead of being deleted, and paper_portfolios.inception_at says
where the current run starts. Dividends credited to a paper portfolio are
rows in paper_cash_flows, so cash = initial - buys + sells - fees + flows.

Revision ID: 0128_paper_reset_archive
Revises: 0127_book_position_snapshots
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0128_paper_reset_archive"
down_revision = "0127_book_position_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("paper_portfolios"):
        columns = {c["name"] for c in inspector.get_columns("paper_portfolios")}
        if "inception_at" not in columns:
            op.add_column("paper_portfolios", sa.Column("inception_at", sa.DateTime(timezone=True), nullable=True))
    if not inspector.has_table("paper_portfolio_archives"):
        op.create_table(
            "paper_portfolio_archives",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("portfolio_id", sa.String(length=36),
                      sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("inception_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reason", sa.String(length=200), nullable=False, server_default="reset"),
            sa.Column("payload_json", sa.Text(), nullable=False),
        )
        op.create_index("ix_paper_portfolio_archives_portfolio_id", "paper_portfolio_archives", ["portfolio_id"])
    if not inspector.has_table("paper_cash_flows"):
        op.create_table(
            "paper_cash_flows",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("portfolio_id", sa.String(length=36),
                      sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("ticker", sa.String(length=32), nullable=False),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("amount_per_unit", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("amount_eur", sa.Numeric(20, 2), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("portfolio_id", "kind", "ticker", "date", name="uq_paper_cash_flow"),
        )
        op.create_index("ix_paper_cash_flows_portfolio_id", "paper_cash_flows", ["portfolio_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("paper_cash_flows"):
        op.drop_index("ix_paper_cash_flows_portfolio_id", table_name="paper_cash_flows")
        op.drop_table("paper_cash_flows")
    if inspector.has_table("paper_portfolio_archives"):
        op.drop_index("ix_paper_portfolio_archives_portfolio_id", table_name="paper_portfolio_archives")
        op.drop_table("paper_portfolio_archives")
    if inspector.has_table("paper_portfolios"):
        columns = {c["name"] for c in inspector.get_columns("paper_portfolios")}
        if "inception_at" in columns:
            with op.batch_alter_table("paper_portfolios") as batch:
                batch.drop_column("inception_at")
