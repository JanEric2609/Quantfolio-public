"""Backfill paper_portfolios.baseline_value after depot-exclusion fix.

Migration 0060 set baseline_value = initial_cash + cost_basis(holdings) but
ran ONCE. Two scenarios leave it wrong:

1. Holdings were seeded AFTER migration 0060 ran — baseline_value was set
   from 0 cost_basis, so it equals initial_cash only (phantom return).

2. initial_cash was inflated by the depot double-count bug (Bug 1) before the
   depot-exclusion fix landed. Migration 0060 then stored an inflated
   baseline_value.

This migration unconditionally recomputes baseline_value for all
paper_portfolio rows that have no trades (safe to correct in-place) and for
rows where baseline_value IS NULL regardless of trade state.

Revision ID: 0063_backfill_baseline_value
Revises: 0062_graduation_assessments
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0063_backfill_baseline_value"
down_revision = "0062_graduation_assessments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("paper_portfolios"):
        return

    cols = [c["name"] for c in inspector.get_columns("paper_portfolios")]
    if "baseline_value" not in cols:
        # Column absent — add it (defensive: migration 0060 should have done this)
        op.add_column(
            "paper_portfolios",
            sa.Column("baseline_value", sa.Numeric(20, 2), nullable=True),
        )

    has_trades = inspector.has_table("paper_trades")
    has_holdings = inspector.has_table("paper_holdings")

    if not has_holdings:
        return

    cost_basis_subquery = (
        "(SELECT COALESCE(SUM(ph.quantity * ph.avg_buy_price), 0) "
        " FROM paper_holdings AS ph WHERE ph.portfolio_id = pp.id)"
    )

    if has_trades:
        # For portfolios WITH trades: only touch rows where baseline_value is
        # still NULL (safe — they have never had a valid basis set).
        op.execute(
            f"""
            UPDATE paper_portfolios AS pp
            SET baseline_value = pp.initial_cash + {cost_basis_subquery}
            WHERE pp.baseline_value IS NULL
            """
        )
        # For portfolios WITHOUT trades: always recompute — these are either
        # freshly seeded or have stale/inflated values from the depot bug.
        op.execute(
            f"""
            UPDATE paper_portfolios AS pp
            SET baseline_value = pp.initial_cash + {cost_basis_subquery}
            WHERE NOT EXISTS (
                SELECT 1 FROM paper_trades AS pt WHERE pt.portfolio_id = pp.id
            )
            """
        )
    else:
        # No trades table yet — recompute unconditionally.
        op.execute(
            f"""
            UPDATE paper_portfolios AS pp
            SET baseline_value = pp.initial_cash + {cost_basis_subquery}
            """
        )


def downgrade() -> None:
    # No structural changes — baseline_value stays; values can only be
    # recalculated forward. Downgrade is a no-op.
    pass
