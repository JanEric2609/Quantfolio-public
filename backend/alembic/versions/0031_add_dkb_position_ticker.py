"""Add ticker column to DkbPosition for ISIN→ticker resolution cache.

Revision ID: 0031_add_dkb_position_ticker
Revises: 0030_dkb_schema_repairs
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa


revision = "0031_add_dkb_position_ticker"
down_revision = "0030_dkb_schema_repairs"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


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
    if not _has_column("dkb_positions", "ticker"):
        op.add_column(
            "dkb_positions",
            sa.Column("ticker", sa.String(32), nullable=True),
        )
        safe_create_index("ix_dkb_positions_ticker", "dkb_positions", ["ticker"])


def downgrade() -> None:
    if _has_column("dkb_positions", "ticker"):
        safe_drop_index("ix_dkb_positions_ticker", table_name="dkb_positions")
        op.drop_column("dkb_positions", "ticker")
