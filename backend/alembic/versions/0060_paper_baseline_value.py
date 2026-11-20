"""Add baseline_value to paper_portfolios (correct return basis).

The return basis was previously ``initial_cash`` (the cash sleeve only), while
seed copied securities into the portfolio for free. That produced a phantom
total return of (cash + securities) / cash. ``baseline_value`` stores the true
starting invested capital = initial cash + cost basis of seeded holdings, so
total_return_pct = (current_total_value - baseline_value) / baseline_value.

Revision ID: 0060_paper_baseline_value
Revises: 0059_portfolio_snap_return_pct
Create Date: 2026-06-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0060_paper_baseline_value"
down_revision = "0059_portfolio_snap_return_pct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("paper_portfolios"):
        return

    cols = [c["name"] for c in inspector.get_columns("paper_portfolios")]
    if "baseline_value" not in cols:
        op.add_column(
            "paper_portfolios",
            sa.Column("baseline_value", sa.Numeric(20, 2), nullable=True),
        )

    # Backfill: baseline = initial_cash + cost basis of currently seeded holdings.
    # Correlated subquery is portable across PostgreSQL and SQLite.
    #
    # Only safe for *untraded* portfolios, where current holdings still equal the
    # seeded state. Once a portfolio has traded, current holdings (and their
    # blended avg_buy_price) no longer reflect the seeded cost basis — buys
    # convert already-counted cash into securities, so summing current holdings
    # would double-count. Leave those NULL so they can be repaired explicitly
    # (e.g. via reseed → recompute_baseline_value) rather than persisting a wrong
    # value here.
    has_trades = inspector.has_table("paper_trades")
    trades_guard = (
        "AND NOT EXISTS (SELECT 1 FROM paper_trades AS pt WHERE pt.portfolio_id = pp.id)"
        if has_trades
        else ""
    )
    op.execute(
        f"""
        UPDATE paper_portfolios AS pp
        SET baseline_value = pp.initial_cash + COALESCE(
            (
                SELECT SUM(ph.quantity * ph.avg_buy_price)
                FROM paper_holdings AS ph
                WHERE ph.portfolio_id = pp.id
            ),
            0
        )
        WHERE pp.baseline_value IS NULL
        {trades_guard}
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("paper_portfolios"):
        cols = [c["name"] for c in inspector.get_columns("paper_portfolios")]
        if "baseline_value" in cols:
            op.drop_column("paper_portfolios", "baseline_value")
