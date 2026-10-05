"""Add paper-portfolio tables for AI paper trading.

Revision ID: 0047_add_paper_portfolio_tables
Revises: 0046_add_user_role
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0047_add_paper_portfolio_tables"
down_revision = "0046_add_user_role"
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

    if not inspector.has_table("paper_portfolios"):
        op.create_table(
            "paper_portfolios",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(120), nullable=False, server_default="AI Paper Portfolio"),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("initial_cash", sa.Numeric(20, 2), nullable=False, server_default=sa.text("100000")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("user_id", name="uq_paper_portfolio_user"),
        )

    if not inspector.has_table("paper_holdings"):
        op.create_table(
            "paper_holdings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("portfolio_id", sa.String(36), sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False),
            sa.Column("isin", sa.String(12), nullable=True),
            sa.Column("ticker", sa.String(32), nullable=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("asset_type", sa.String(24), nullable=False, server_default="stock"),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("avg_buy_price", sa.Numeric(20, 6), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        safe_create_index("ix_paper_holdings_isin", "paper_holdings", ["isin"])
        safe_create_index("ix_paper_holdings_ticker", "paper_holdings", ["ticker"])

    if not inspector.has_table("paper_trades"):
        op.create_table(
            "paper_trades",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("portfolio_id", sa.String(36), sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False),
            sa.Column("holding_id", sa.String(36), sa.ForeignKey("paper_holdings.id", ondelete="SET NULL"), nullable=True),
            sa.Column("ticker", sa.String(32), nullable=False),
            sa.Column("side", sa.String(4), nullable=False),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("price", sa.Numeric(20, 6), nullable=False),
            sa.Column("value", sa.Numeric(20, 2), nullable=False),
            sa.Column("date", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("confidence", sa.Float, nullable=True),
            sa.Column("rationale", sa.Text, nullable=True),
            sa.Column("ai_decision_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )

    if not inspector.has_table("paper_snapshots"):
        op.create_table(
            "paper_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("portfolio_id", sa.String(36), sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False),
            sa.Column("date", sa.Date, nullable=False),
            sa.Column("total_value", sa.Numeric(20, 2), nullable=False, server_default=sa.text("0")),
            sa.Column("cash_balance", sa.Numeric(20, 2), nullable=False, server_default=sa.text("0")),
            sa.Column("securities_value", sa.Numeric(20, 2), nullable=False, server_default=sa.text("0")),
            sa.Column("total_return_pct", sa.Numeric(8, 4), nullable=False, server_default=sa.text("0")),
            sa.Column("sharpe", sa.Float, nullable=True),
            sa.Column("max_drawdown", sa.Float, nullable=True),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("portfolio_id", "date", name="uq_paper_snapshots_portfolio_date"),
        )
        # Unique constraint uq_paper_snapshots_portfolio_date already provides an index for (portfolio_id, date)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("paper_snapshots"):
        indexes = {idx["name"] for idx in inspector.get_indexes("paper_snapshots")}
        if "ix_paper_snapshots_portfolio_date" in indexes:
            safe_drop_index("ix_paper_snapshots_portfolio_date", table_name="paper_snapshots")
        op.drop_table("paper_snapshots")

    if inspector.has_table("paper_trades"):
        op.drop_table("paper_trades")

    if inspector.has_table("paper_holdings"):
        indexes = {idx["name"] for idx in inspector.get_indexes("paper_holdings")}
        if "ix_paper_holdings_isin" in indexes:
            safe_drop_index("ix_paper_holdings_isin", table_name="paper_holdings")
        if "ix_paper_holdings_ticker" in indexes:
            safe_drop_index("ix_paper_holdings_ticker", table_name="paper_holdings")
        op.drop_table("paper_holdings")

    if inspector.has_table("paper_portfolios"):
        op.drop_table("paper_portfolios")
