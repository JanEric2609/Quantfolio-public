"""Remove duplicate news items and make news_items.url unique.

The news refresh stored an RSS article twice when two portfolio symbols
matched it in the same run (EUNL.DE and IWDA.L are the same fund) or a
feed listed it twice: the session does not autoflush, so the second lookup
missed the first, still pending, insert. From then on every refresh raised
MultipleResultsFound for that URL (prod: 5 duplicated URLs, a warning per
ETF every 4 hours). The code now deduplicates; the unique index stops a
concurrent refresh from reintroducing a duplicate.

Of each duplicated URL the earliest fetched row is kept. The others' relevance
tags are removed with them (user_news_relevance cascades, but SQLite does
not enforce that without PRAGMA foreign_keys).

Revision ID: 0114_news_url_unique
Revises: 0113_refetch_fundamentals
"""
from alembic import op
import sqlalchemy as sa

revision = "0114_news_url_unique"
down_revision = "0113_refetch_fundamentals"
branch_labels = None
depends_on = None

INDEX = "uq_news_items_url"

# A row is a duplicate when another row has the same URL and was fetched
# earlier (ties broken by id).
_DUPLICATE_IDS = """
    SELECT n.id FROM news_items n
    WHERE EXISTS (
        SELECT 1 FROM news_items k
        WHERE k.url = n.url
          AND (k.fetched_at < n.fetched_at OR (k.fetched_at = n.fetched_at AND k.id < n.id))
    )
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("news_items"):
        return
    if inspector.has_table("user_news_relevance"):
        bind.execute(sa.text(f"DELETE FROM user_news_relevance WHERE news_item_id IN ({_DUPLICATE_IDS})"))
    bind.execute(sa.text(f"DELETE FROM news_items WHERE id IN ({_DUPLICATE_IDS})"))
    if INDEX not in {ix["name"] for ix in inspector.get_indexes("news_items")}:
        op.create_index(INDEX, "news_items", ["url"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("news_items") and INDEX in {ix["name"] for ix in inspector.get_indexes("news_items")}:
        op.drop_index(INDEX, table_name="news_items")
