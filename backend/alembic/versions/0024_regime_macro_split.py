"""Phase 9: create macro_indicators table (regime/macro split)

Revision ID: 0024_regime_macro_split
Revises: 0023_fix_ledger_pk
Create Date: 2026-05-30

Creates the macro_indicators table (previously only declared as an ORM model
with no migration). Macro ingest is repointed here so regime_snapshots stays
regime-only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_regime_macro_split"
down_revision = "0023_fix_ledger_pk"
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
    if not inspector.has_table("macro_indicators"):
        op.create_table(
            "macro_indicators",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("value", sa.Numeric(20, 6), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", "date", "source", name="uq_macro_name_date_source"),
        )
        safe_create_index("ix_macro_indicators_name", "macro_indicators", ["name"])
        safe_create_index("ix_macro_indicators_date", "macro_indicators", ["date"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("macro_indicators"):
        safe_drop_index("ix_macro_indicators_date", table_name="macro_indicators")
        safe_drop_index("ix_macro_indicators_name", table_name="macro_indicators")
        op.drop_table("macro_indicators")
