"""pdmr_transactions: managers' transactions from national registers (Sweden first).

Revision ID: 0160_pdmr_transactions
Revises: 0159_cfd_journal_wider
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0160_pdmr_transactions"
down_revision = "0159_cfd_journal_wider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("pdmr_transactions"):
        return
    op.create_table(
        "pdmr_transactions",
        sa.Column("row_key", sa.String(40), primary_key=True),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("isin", sa.String(12), nullable=True),
        sa.Column("issuer", sa.String(200), nullable=False),
        sa.Column("person", sa.String(200), nullable=False),
        sa.Column("position", sa.String(120), nullable=False, server_default=""),
        sa.Column("closely_associated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("nature", sa.String(60), nullable=False, server_default=""),
        sa.Column("instrument_type", sa.String(60), nullable=False, server_default=""),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("source", "isin", "side", "transaction_date", "publication_date"):
        op.create_index(f"ix_pdmr_transactions_{column}", "pdmr_transactions", [column])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("pdmr_transactions"):
        op.drop_table("pdmr_transactions")
