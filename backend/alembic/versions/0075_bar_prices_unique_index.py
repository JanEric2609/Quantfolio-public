"""Dedupe bar_prices / factor_loadings_daily and add business-key unique indexes.

These hypertables were created (0015) with a composite PK of (id, ts) only.
Because id is auto-generated, the ON CONFLICT DO NOTHING in DataIngester /
FactorsStore could never fire, so every re-ingest (the daily 1-day backfill
overlap, and the discover pipeline's full 5-year re-pull of ~236 symbols)
inserted fully duplicated rows. Duplicate closes silently corrupt every return /
volatility / Sharpe / drawdown computed from these bars.

This migration:
  1. Deletes duplicate rows, keeping the most-recently-inserted (max id) per
     business key.
  2. Adds a UNIQUE index on the business key so the new
     ON CONFLICT (symbol, ts) / (symbol, factor, ts) upserts work. TimescaleDB
     requires the partition column (ts) to be part of any unique index, which it
     is here.

NOTE: step 1 mutates existing production data. Applying to prod requires an
explicit go-ahead (per the review constraints); the migration is idempotent and
safe to re-run.

Revision ID: 0075_bar_prices_unique_index
Revises: 0074_ledger_mc_id_defaults
"""
from alembic import op
import sqlalchemy as sa

revision = "0075_bar_prices_unique_index"
down_revision = "0074_ledger_mc_id_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # bar_prices / factor_loadings_daily have no ORM model, so they do not
        # exist in the SQLite test DB (create_all skips them). Nothing to do.
        return
    inspector = sa.inspect(bind)

    if inspector.has_table("bar_prices"):
        bind.execute(sa.text(
            "DELETE FROM bar_prices a USING bar_prices b "
            "WHERE a.symbol = b.symbol AND a.ts = b.ts AND a.id < b.id"
        ))
        bind.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bar_prices_symbol_ts "
            "ON bar_prices (symbol, ts)"
        ))

    if inspector.has_table("factor_loadings_daily"):
        bind.execute(sa.text(
            "DELETE FROM factor_loadings_daily a USING factor_loadings_daily b "
            "WHERE a.symbol = b.symbol AND a.factor = b.factor AND a.ts = b.ts AND a.id < b.id"
        ))
        bind.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_loadings_symbol_factor_ts "
            "ON factor_loadings_daily (symbol, factor, ts)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_bar_prices_symbol_ts"))
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_factor_loadings_symbol_factor_ts"))
