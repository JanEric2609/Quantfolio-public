"""create TimescaleDB hypertables for time-series data

Revision ID: 0015_hypertables
Revises: 0014_timescale_enable
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_hypertables"
down_revision = "0014_timescale_enable"
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

    # Create bar_prices hypertable
    if not inspector.has_table("bar_prices"):
        op.create_table(
            "bar_prices",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("open", sa.Float(), nullable=False),
            sa.Column("high", sa.Float(), nullable=False),
            sa.Column("low", sa.Float(), nullable=False),
            sa.Column("close", sa.Float(), nullable=False),
            sa.Column("volume", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.PrimaryKeyConstraint("id", "ts"),
        )
        safe_create_index("ix_bar_prices_symbol_ts", "bar_prices", ["symbol", sa.desc("ts")])

        # Convert to hypertable with 7-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('bar_prices', 'ts',
                                         chunk_time_interval => interval '7 days',
                                         if_not_exists => TRUE)
            """))

    # Create factor_loadings_daily hypertable
    if not inspector.has_table("factor_loadings_daily"):
        op.create_table(
            "factor_loadings_daily",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("factor", sa.String(length=80), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("loading", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("id", "ts"),
        )
        safe_create_index("ix_factor_loadings_daily_factor_ts", "factor_loadings_daily",
                       ["factor", sa.desc("ts")])
        safe_create_index("ix_factor_loadings_daily_symbol_factor_ts", "factor_loadings_daily",
                       ["symbol", "factor", sa.desc("ts")])

        # Convert to hypertable with 30-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('factor_loadings_daily', 'ts',
                                         chunk_time_interval => interval '30 days',
                                         if_not_exists => TRUE)
            """))

    # Create regime_snapshots hypertable
    if not inspector.has_table("regime_snapshots"):
        op.create_table(
            "regime_snapshots",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("label", sa.String(length=80), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.PrimaryKeyConstraint("id", "ts"),
        )
        safe_create_index("ix_regime_snapshots_label_ts", "regime_snapshots",
                       ["label", sa.desc("ts")])

        # Convert to hypertable with 30-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('regime_snapshots', 'ts',
                                         chunk_time_interval => interval '30 days',
                                         if_not_exists => TRUE)
            """))

    # Create provider_health_history hypertable
    if not inspector.has_table("provider_health_history"):
        op.create_table(
            "provider_health_history",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("capability", sa.String(length=80), nullable=False),
            sa.Column("ok", sa.Boolean(), nullable=False),
            sa.Column("latency_ms", sa.Float(), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id", "ts"),
        )
        safe_create_index("ix_provider_health_history_provider_ts", "provider_health_history",
                       ["provider", sa.desc("ts")])

        # Convert to hypertable with 7-day chunks (PostgreSQL only)
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("""
                SELECT create_hypertable('provider_health_history', 'ts',
                                         chunk_time_interval => interval '7 days',
                                         if_not_exists => TRUE)
            """))

    # Create continuous aggregates for bar_prices (PostgreSQL only).
    # They run on the Alembic connection, inside its transaction: WITH NO DATA
    # aggregates are transactional DDL, and an autocommit side connection would
    # not see the not-yet-committed bar_prices (see 2ce7981ac5c7_bar_prices_caggs).
    # The previous psycopg2-only ``set_isolation_level`` trick also fails on
    # psycopg 3, which made a fresh install stop at this migration.
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS bar_prices_weekly
            WITH (timescaledb.continuous) AS
            SELECT
                symbol,
                time_bucket('7 days', ts) AS week_ts,
                first(open, ts) AS open,
                max(high) AS high,
                min(low) AS low,
                last(close, ts) AS close,
                sum(volume) AS volume,
                currency,
                last(provider, ts) AS provider
            FROM bar_prices
            GROUP BY symbol, week_ts, currency
            WITH NO DATA;
        """))
        bind.execute(sa.text("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS bar_prices_monthly
            WITH (timescaledb.continuous) AS
            SELECT
                symbol,
                time_bucket('30 days', ts) AS month_ts,
                first(open, ts) AS open,
                max(high) AS high,
                min(low) AS low,
                last(close, ts) AS close,
                sum(volume) AS volume,
                currency,
                last(provider, ts) AS provider
            FROM bar_prices
            GROUP BY symbol, month_ts, currency
            WITH NO DATA;
        """))
        bind.execute(sa.text("""
            SELECT add_retention_policy('bar_prices', interval '10 years', if_not_exists => TRUE);
        """))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop continuous aggregates
    if inspector.has_table("bar_prices_monthly"):
        bind.execute(sa.text("DROP MATERIALIZED VIEW IF EXISTS bar_prices_monthly CASCADE"))

    if inspector.has_table("bar_prices_weekly"):
        bind.execute(sa.text("DROP MATERIALIZED VIEW IF EXISTS bar_prices_weekly CASCADE"))

    # Drop hypertables (and their data)
    if inspector.has_table("provider_health_history"):
        op.drop_table("provider_health_history")

    if inspector.has_table("regime_snapshots"):
        op.drop_table("regime_snapshots")

    if inspector.has_table("factor_loadings_daily"):
        op.drop_table("factor_loadings_daily")

    if inspector.has_table("bar_prices"):
        op.drop_table("bar_prices")
