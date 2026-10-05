"""Add index on news_items.published_at (primary filter+sort column on news endpoints).

Revision ID: 0071_news_published_at_idx
Revises: 0070_float_to_numeric
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa


revision = "0071_news_published_at_idx"
down_revision = "0070_float_to_numeric"
branch_labels = None
depends_on = None


def _index_names(table_name):
    """Return the set of existing index names on ``table_name`` (empty if the table is gone)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def safe_create_index(index_name, table_name, columns, unique=False, **kwargs):
    """Create an index only if it does not already exist (idempotent / duplicate-safe)."""
    resolved = op.f(index_name)
    if resolved not in _index_names(table_name):
        op.create_index(resolved, table_name, columns, unique=unique, **kwargs)


def safe_drop_index(index_name, table_name=None, **kwargs):
    """Drop an index only if it exists (idempotent / safe on re-run)."""
    resolved = op.f(index_name)
    if table_name is not None and resolved in _index_names(table_name):
        op.drop_index(resolved, table_name=table_name, **kwargs)
    elif table_name is None:
        op.drop_index(resolved, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("news_items"):
        return
    existing = {ix["name"] for ix in inspector.get_indexes("news_items")}
    if "ix_news_items_published_at" not in existing:
        safe_create_index("ix_news_items_published_at", "news_items", ["published_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("news_items"):
        return
    if any(ix["name"] == "ix_news_items_published_at" for ix in inspector.get_indexes("news_items")):
        safe_drop_index("ix_news_items_published_at", table_name="news_items")
