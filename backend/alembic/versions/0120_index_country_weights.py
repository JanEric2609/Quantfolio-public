"""index_currency_weights.country_weights_json: an index's country weights.

The ETF page's geographic breakdown read yfinance's ``country_weightings``,
which yfinance has never had, so it was always empty unless yfinance failed
outright and a hard-coded 2024-ish split showed instead (EUNL.DE "65% North
America"; MSCI World was ~76% US and Canada on 2026-09-29). The SPDR holdings
files the monthly refresh already downloads list each constituent's country,
so the refresh now stores those weights next to the currency weights.

Revision ID: 0120_index_country_weights
Revises: 0119_analyst_estimate_snapshots
"""
from alembic import op
import sqlalchemy as sa

revision = "0120_index_country_weights"
down_revision = "0119_analyst_estimate_snapshots"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(table) and column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("index_currency_weights") and not _has_column("index_currency_weights", "country_weights_json"):
        op.add_column("index_currency_weights", sa.Column("country_weights_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("index_currency_weights", "country_weights_json"):
        with op.batch_alter_table("index_currency_weights") as batch:
            batch.drop_column("country_weights_json")
