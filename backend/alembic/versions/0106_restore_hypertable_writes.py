"""Restore hypertable write path (regression fix-forward).

ADR 0014. ``2ce7981ac5c6_recreate_analytics`` recreated ``bar_prices``,
``factor_loadings_daily``, ``regime_snapshots`` and ``provider_health_history``
from scratch (after ``2ce7981ac5c5`` dropped them) using bare
``sa.Column("id", sa.BIGINT(), nullable=False)`` and non-unique indexes only.
That silently discarded two earlier, already-applied fixes:

  - ``0073_hypertable_id_defaults`` — attached identity sequences to ``id`` so
    inserts that omit it (all of them: DataIngester, BarStore) don't raise
    NotNullViolation.
  - ``0075_bar_prices_unique_index`` — added the business-key UNIQUE indexes
    the ``ON CONFLICT (symbol, ts)`` / ``(symbol, factor, ts)`` upserts name.

Verified live (2026-09-01, via read-only Postgres query): all four tables had
0 rows, no ``id`` default, and only the non-unique ``ix_*`` indexes — every
insert failed with ``NotNullViolation``/``InvalidColumnReference``, caught by
a bare ``except Exception`` in ``DataIngester.ingest_bar_prices`` that reports
the failure into ``provider_health_history`` — one of the four broken tables,
so the outage was invisible until this forensics pass.

This migration re-applies 0073 + 0075's exact patterns to all four tables
(0075 only covered two). ``regime_snapshots`` and ``provider_health_history``
are insert-only (no business-key upsert exists for them), so they only get
the ``id`` default, not a unique index.

A future migration that recreates any of these tables from scratch under a
``has_table`` guard MUST re-run this migration's effects too, or it will
regress again exactly as ``2ce7981ac5c6`` did.

Revision ID: 0106_restore_hypertable_writes
Revises: 2ce7981ac5c7_bar_prices_caggs
"""
from alembic import op
import sqlalchemy as sa

revision = "0106_restore_hypertable_writes"
down_revision = "2ce7981ac5c7_bar_prices_caggs"
branch_labels = None
depends_on = None

_ID_DEFAULT_TABLES = [
    "bar_prices",
    "factor_loadings_daily",
    "regime_snapshots",
    "provider_health_history",
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # These hypertables have no ORM model; SQLite test DBs never create
        # them (create_all skips them). Nothing to do.
        return
    inspector = sa.inspect(bind)

    for table in _ID_DEFAULT_TABLES:
        if not inspector.has_table(table):
            continue
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        id_col = columns.get("id")
        if id_col is None or id_col.get("default"):
            continue
        seq = f"{table}_id_seq"
        bind.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {seq} OWNED BY {table}.id"))
        bind.execute(sa.text(
            f"SELECT setval('{seq}', COALESCE((SELECT max(id) FROM {table}), 0) + 1, false)"
        ))
        bind.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{seq}')"
        ))

    if inspector.has_table("bar_prices"):
        bind.execute(sa.text(
            "DELETE FROM bar_prices a USING bar_prices b "
            "WHERE a.symbol = b.symbol AND a.ts = b.ts AND a.id < b.id"
        ))
        bind.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bar_prices_symbol_ts "
            "ON bar_prices (symbol, ts)"
        ))

    if inspector.has_table("factor_loadings_daily"):
        bind.execute(sa.text(
            "DELETE FROM factor_loadings_daily a USING factor_loadings_daily b "
            "WHERE a.symbol = b.symbol AND a.factor = b.factor AND a.ts = b.ts AND a.id < b.id"
        ))
        bind.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_loadings_symbol_factor_ts "
            "ON factor_loadings_daily (symbol, factor, ts)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_bar_prices_symbol_ts"))
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_factor_loadings_symbol_factor_ts"))
    inspector = sa.inspect(bind)
    for table in _ID_DEFAULT_TABLES:
        if not inspector.has_table(table):
            continue
        bind.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT"))
        bind.execute(sa.text(f"DROP SEQUENCE IF EXISTS {table}_id_seq"))
