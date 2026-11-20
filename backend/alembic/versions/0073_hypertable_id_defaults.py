"""Attach identity sequences to hypertable id columns.

Migration 0015 created the TimescaleDB hypertables with a composite primary
key (id, ts). Composite PKs get no autoincrement from SQLAlchemy, so the id
columns ended up NOT NULL with no default — every INSERT that omits id (all
of them: DataIngester, BarStore) failed with NotNullViolation. bar_prices
had 0 rows in production as a result, permanently failing the PRICED_HISTORY
buy gate.

Revision ID: 0073_hypertable_id_defaults
Revises: 0072_alphacrafter_user_scope
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_hypertable_id_defaults"
down_revision = "0072_alphacrafter_user_scope"
branch_labels = None
depends_on = None

_TABLES = [
    "bar_prices",
    "factor_loadings_daily",
    "regime_snapshots",
    "provider_health_history",
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) creates these tables ad hoc without the id column.
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
