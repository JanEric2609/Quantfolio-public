"""news_items.sentiment_source and news_items.feed_ticker.

Plan 2026-11-10, WP5. ``sentiment_source`` says who scored an article
(finbert / provider / lexicon); readers treat lexicon rows as unscored.
``feed_ticker`` keeps the symbol whose feed returned the article, so ``ticker``
can mean "the company the article names". Additive only.

Revision ID: 0151_news_source_feed_ticker
Revises: 0150_venue_instruments
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0151_news_source_feed_ticker"
down_revision = "0150_venue_instruments"
branch_labels = None
depends_on = None

_COLUMNS = (("sentiment_source", 16), ("feed_ticker", 32))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("news_items"):
        return
    have = {c["name"] for c in inspector.get_columns("news_items")}
    for name, length in _COLUMNS:
        if name not in have:
            op.add_column("news_items", sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("news_items"):
        return
    have = {c["name"] for c in inspector.get_columns("news_items")}
    with op.batch_alter_table("news_items") as batch:
        for name, _ in _COLUMNS:
            if name in have:
                batch.drop_column(name)
