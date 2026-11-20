"""broker_instruments: Trading 212's instrument list (ISIN, ticker, currency).

Additive only: no existing table changes.

Revision ID: 0143_broker_instruments
Revises: 0142_broker_pies
"""
from alembic import op
import sqlalchemy as sa

revision = "0143_broker_instruments"
down_revision = "0142_broker_pies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("broker_instruments"):
        return
    op.create_table(
        "broker_instruments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("ticker", sa.String(40), nullable=False),
        sa.Column("isin", sa.String(12), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("type", sa.String(24), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "ticker", name="uq_broker_instruments_source_ticker"),
    )
    op.create_index("ix_broker_instruments_source_isin", "broker_instruments", ["source", "isin"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("broker_instruments"):
        op.drop_index("ix_broker_instruments_source_isin", table_name="broker_instruments")
        op.drop_table("broker_instruments")
