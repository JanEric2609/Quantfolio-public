"""index_currency_weights: the currencies an index's constituents trade in.

Verification treated a UCITS ETF as exposed to the currency it is quoted in,
so a portfolio holding only EUNL.DE (MSCI World on Xetra, in EUR) showed no
dollar exposure although ~73% of MSCI World trades in USD. Rows are refreshed
monthly from a tracking fund's published holdings; until the first refresh,
``foundation.etf_currency`` uses its seed values.

Revision ID: 0118_index_currency_weights
Revises: 0117_uncalibrated_is_null
"""
from alembic import op
import sqlalchemy as sa

revision = "0118_index_currency_weights"
down_revision = "0117_uncalibrated_is_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("index_currency_weights"):
        op.create_table(
            "index_currency_weights",
            sa.Column("index_key", sa.String(length=32), primary_key=True),
            sa.Column("weights_json", sa.JSON(), nullable=False),
            sa.Column("as_of", sa.Date(), nullable=False),
            sa.Column("source", sa.String(length=200), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("index_currency_weights"):
        op.drop_table("index_currency_weights")
