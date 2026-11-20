"""Re-fetch Finnhub fundamentals cached with last fiscal year's ratios.

Finnhub fundamentals (first in the chain for US listings) mapped P/E from
``peNormalizedAnnual`` (current price over the last fiscal year's EPS), P/B
from ``pbAnnual`` (the fiscal year-end price over book) and debt/equity from
the annual balance sheet. After a year of fast growth that is badly stale:
MU read P/E 143 and P/B 2.5 against a trailing-twelve-month P/E of 24 and a
current P/B of 12 (prod, 2026-09-28). The provider now reads the TTM and
current fields, but cached rows would keep the old values for up to
``FUNDAMENTALS_TTL`` (7 days).

Finnhub rows are backdated so the next read re-fetches; unlike 0113 they are
kept, so a failed live fetch can still fall back to them. The table is a
cache, so the downgrade is a no-op.

Revision ID: 0115_refetch_finnhub_ratios
Revises: 0114_news_url_unique
"""
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa

revision = "0115_refetch_finnhub_ratios"
down_revision = "0114_news_url_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("fundamentals"):
        return
    bind.execute(
        sa.text("UPDATE fundamentals SET stale = :stale, fetched_at = :epoch WHERE source = 'finnhub'"),
        {"stale": True, "epoch": datetime(2000, 1, 1, tzinfo=UTC)},
    )


def downgrade() -> None:
    pass
