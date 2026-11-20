"""Add fee column to paper_trades.

The paper sleeve modelled zero transaction costs, so the advisor loop learned
that churn is free. Fees are now charged on both sides (buy costs value+fee,
sell returns value-fee) and the per-trade amount is persisted so the cash
balance stays reconstructible from the trade log.

Revision ID: 0087_paper_trade_fee
Revises: 0086_holdings_avg_buy_nullable
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0087_paper_trade_fee"
down_revision = "0086_holdings_avg_buy_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("paper_trades"):
        return
    columns = {col["name"] for col in inspector.get_columns("paper_trades")}
    if "fee" not in columns:
        op.add_column(
            "paper_trades",
            sa.Column(
                "fee",
                sa.Numeric(20, 2),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("paper_trades"):
        return
    columns = {col["name"] for col in inspector.get_columns("paper_trades")}
    if "fee" in columns:
        op.drop_column("paper_trades", "fee")
