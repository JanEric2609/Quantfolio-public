"""Tax ledger dedupe: source_ref unique index for DKB dividend ingestion.

audit-fixes-2026-08 todo 25. The nullable ``source_ref`` column already exists
on ``tax_ledger_events`` (the manual POST /api/tax/events writer has always
accepted it); this migration adds it only where a legacy DB lacks it
(has_column guard) and adds the unique index that makes ingestion idempotent.
Multiple NULLs are permitted by both SQLite and PostgreSQL, so manual events
without a source_ref remain valid.

Revision ID: 0101_tax_ledger_dedupe
Revises: 0099_purge_experimental_setting
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0101_tax_ledger_dedupe"
down_revision = "0099_purge_experimental_setting"
branch_labels = None
depends_on = None

_TABLE = "tax_ledger_events"
_COLUMN = "source_ref"
_INDEX = "uq_tax_ledger_source_ref"


def _columns(inspector: sa.Inspector) -> set[str]:
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def _indexes(inspector: sa.Inspector) -> set[str]:
    return {idx["name"] for idx in inspector.get_indexes(_TABLE)}


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
    if not inspector.has_table(_TABLE):
        return
    if _COLUMN not in _columns(inspector):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=120), nullable=True))
    if _INDEX not in _indexes(inspector):
        safe_create_index(_INDEX, _TABLE, [_COLUMN], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    if _INDEX in _indexes(inspector):
        safe_drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN in _columns(inspector):
        op.drop_column(_TABLE, _COLUMN)
