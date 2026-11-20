"""Record what each paper trade traded and a sale's cost basis (plan 2026-10-14, M2).

Revision ID: 0138_paper_trade_asset
Revises: 0137_paper_trade_checks
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0138_paper_trade_asset"
down_revision = "0137_paper_trade_checks"
branch_labels = None
depends_on = None

_TABLE = "paper_trades"
_COLUMNS = (
    ("isin", sa.String(12)),
    ("asset_type", sa.String(24)),
    ("avg_cost", sa.Numeric(20, 6)),
)


def _existing() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return set()
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    have = _existing()
    if not have:
        return
    for name, type_ in _COLUMNS:
        if name not in have:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    have = _existing()
    if not have & {name for name, _ in _COLUMNS}:
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name, _ in reversed(_COLUMNS):
            if name in have:
                batch.drop_column(name)
