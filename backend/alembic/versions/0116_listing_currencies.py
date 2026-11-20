"""listing_currencies: the quote unit each symbol's stored prices are in.

``bar_prices.currency`` came from the ``assets`` table (a fund's base
currency) or an "EUR" fallback, so 429 of 780 symbols were mislabelled
(2026-09-28). Price readers now resolve the unit from this table, filled with
the provider-reported currency by ingestion and by the listing-currency audit
job, before falling back to the listing suffix.

The table starts empty: the audit runs at worker start.

Revision ID: 0116_listing_currencies
Revises: 0115_refetch_finnhub_ratios
"""
from alembic import op
import sqlalchemy as sa

revision = "0116_listing_currencies"
down_revision = "0115_refetch_finnhub_ratios"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("listing_currencies"):
        op.create_table(
            "listing_currencies",
            sa.Column("symbol", sa.String(length=32), primary_key=True),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("listing_currencies"):
        op.drop_table("listing_currencies")
