"""Delete the zero-valued macro fallback rows (plan 2026-10-12, D11).

``market.macro_indicators`` inserted "ECB deposit rate" and "Euro area CPI"
rows with value 0 and ``source='fallback'`` whenever the table was empty;
they were read as real data. The code no longer writes them. Data only: the
downgrade cannot bring back rows that were wrong, so it does nothing.

Revision ID: 0135_drop_macro_fallback
Revises: 0134_bar_quarantine
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0135_drop_macro_fallback"
down_revision = "0134_bar_quarantine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("macro_indicators"):
        bind.execute(sa.text("DELETE FROM macro_indicators WHERE source = 'fallback'"))


def downgrade() -> None:
    pass
