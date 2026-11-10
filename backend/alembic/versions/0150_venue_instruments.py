"""venue_instruments: the instruments Xetra and Börse Frankfurt list, by ISIN.

Plan 2026-11-10, WP3. Additive only.

Revision ID: 0150_venue_instruments
Revises: 0149_prediction_superseded
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0150_venue_instruments"
down_revision = "0149_prediction_superseded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("venue_instruments"):
        op.create_table(
            "venue_instruments",
            sa.Column("venue", sa.String(16), primary_key=True),
            sa.Column("isin", sa.String(12), primary_key=True),
            sa.Column("name", sa.String(200), nullable=True),
            sa.Column("currency", sa.String(3), nullable=True),
            sa.Column("as_of", sa.Date(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("venue_instruments"):
        op.drop_table("venue_instruments")
