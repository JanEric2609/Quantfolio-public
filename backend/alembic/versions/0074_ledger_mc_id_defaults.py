"""Attach identity sequences to performance_ledger_entries and mc_path_samples ids.

Follows the same reasoning as 0073_hypertable_id_defaults: migrations 0016/0017
created these tables with a composite primary key ((id, ts) / (id, as_of)) for
TimescaleDB partitioning, which gives the id column no autoincrement/default.
Every INSERT that omits id (write_ledger_snapshot, MC path storage) then fails
with NotNullViolation on a fresh Postgres install. 0073 fixed the same class of
bug for four other hypertables but omitted these two.

Production was hand-patched during the July incident, so this migration is
idempotent (CREATE SEQUENCE IF NOT EXISTS + only sets a default when missing).

Revision ID: 0074_ledger_mc_id_defaults
Revises: 0073_hypertable_id_defaults
"""
from alembic import op
import sqlalchemy as sa

revision = "0074_ledger_mc_id_defaults"
down_revision = "0073_hypertable_id_defaults"
branch_labels = None
depends_on = None

_TABLES = [
    "performance_ledger_entries",
    "mc_path_samples",
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) uses the ORM's BigInteger().with_variant(Integer, "sqlite")
        # id, which autoincrements as a rowid alias — no server default needed.
        return
    inspector = sa.inspect(bind)
    for table in _TABLES:
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


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        bind.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT"))
        bind.execute(sa.text(f"DROP SEQUENCE IF EXISTS {table}_id_seq"))
