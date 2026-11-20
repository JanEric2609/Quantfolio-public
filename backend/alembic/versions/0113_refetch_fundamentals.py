"""Force a re-fetch of cached fundamentals written in the wrong units.

Until this release Finnhub fundamentals (first in the chain for US listings)
were cached with market cap in millions, ratios in percent and no volume,
and yfinance's ``dividendYield`` (a percentage since yfinance 0.2.54) was
stored 100x too high for yields under 0.5%. The universe screen's shallow
``universe_screen`` stubs copied the same Finnhub market cap. Both providers
now return yfinance units, but cached rows would keep serving the old values
for up to ``FUNDAMENTALS_TTL`` (7 days): AAPL/MSFT/NVDA would stay out of the
Discover universe for another week.

Finnhub and ``universe_screen`` rows are deleted, so a failed live fetch can
never fall back to them; every other row is marked stale and backdated so the
next read re-fetches but can still fall back to it. The table is a cache, so
the downgrade is a no-op.

Revision ID: 0113_refetch_fundamentals
Revises: 0112_adr17_signal_weights
"""
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa

revision = "0113_refetch_fundamentals"
down_revision = "0112_adr17_signal_weights"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("fundamentals"):
        return
    bind.execute(sa.text("DELETE FROM fundamentals WHERE source IN ('finnhub', 'universe_screen')"))
    bind.execute(
        sa.text("UPDATE fundamentals SET stale = :stale, fetched_at = :epoch"),
        {"stale": True, "epoch": datetime(2000, 1, 1, tzinfo=UTC)},
    )


def downgrade() -> None:
    pass
