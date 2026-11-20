"""The trade check's shadow verdict on each paper trade (trade-check plan 2026-10-14).

Revision ID: 0137_paper_trade_checks
Revises: 0136_app_errors
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0137_paper_trade_checks"
down_revision = "0136_app_errors"
branch_labels = None
depends_on = None

_TABLE = "paper_trade_checks"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trade_id", sa.String(36), sa.ForeignKey("paper_trades.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("portfolio_id", sa.String(36), sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("traded_on", sa.Date(), nullable=False),
            sa.Column("ticker", sa.String(32), nullable=False),
            sa.Column("side", sa.String(4), nullable=False),
            sa.Column("notional_eur", sa.Float(), nullable=False),
            sa.Column("fee_eur", sa.Float(), nullable=False, server_default="0"),
            sa.Column("spread_eur", sa.Float(), nullable=False, server_default="0"),
            sa.Column("tax_eur", sa.Float(), nullable=False, server_default="0"),
            sa.Column("cost_eur", sa.Float(), nullable=False, server_default="0"),
            sa.Column("p_hit", sa.Float(), nullable=True),
            sa.Column("p_source", sa.String(16), nullable=False),
            sa.Column("mean_abs_excess", sa.Float(), nullable=False),
            sa.Column("edge_eur", sa.Float(), nullable=False),
            sa.Column("verdict", sa.String(8), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("horizon_end", sa.Date(), nullable=False),
            sa.Column("asset_return", sa.Float(), nullable=True),
            sa.Column("benchmark_return", sa.Float(), nullable=True),
            sa.Column("contribution_eur", sa.Float(), nullable=True),
            sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_paper_trade_checks_trade_id", _TABLE, ["trade_id"], unique=True)
        op.create_index("ix_paper_trade_checks_portfolio_id", _TABLE, ["portfolio_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.drop_index("ix_paper_trade_checks_portfolio_id", table_name=_TABLE)
    op.drop_index("ix_paper_trade_checks_trade_id", table_name=_TABLE)
    op.drop_table(_TABLE)
