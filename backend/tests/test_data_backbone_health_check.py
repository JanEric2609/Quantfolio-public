"""Tests for the ADR 0014 hypertable write-invariant health probe.

test_migrations.py already pins that migration 0106_restore_hypertable_writes
lands the id-default + unique-index invariants on a real Postgres after
``alembic upgrade head``. This file instead tests the *runtime* probe
(``check_write_invariants``) that now runs on every app startup and every
``/healthz`` call — proving the probe itself correctly detects both the
healthy and the regressed (ADR 0014 incident) state, not just that the
migration once applied them.
"""
import os

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_backbone.health_check import check_critical_tables, check_write_invariants


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_check_write_invariants_skips_non_postgres():
    db = _memory_db()
    result = check_write_invariants(db)
    assert result["ok"] is True
    assert result["skipped"] == "non-postgres dialect"


def test_check_critical_tables_missing_on_empty_memory_db():
    db = _memory_db()
    result = check_critical_tables(db)
    assert result["ok"] is False
    assert "bar_prices" in result["missing"]


def _live_postgres_url() -> str:
    return os.environ.get(
        "MIGRATION_TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/quantfolio",
    )


@pytest.fixture
def live_postgres_session():
    url = _live_postgres_url()
    try:
        engine = sa.create_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception:
        pytest.skip(f"no reachable Postgres at {url!r}; skipping live health-check test")

    schema = f"healthchktest_{os.getpid()}"
    with engine.begin() as conn:
        conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(sa.text(f"CREATE SCHEMA {schema}"))

    scoped_url = url + ("&" if "?" in url else "?") + f"options=-csearch_path%3D{schema}"
    scoped_engine = sa.create_engine(scoped_url)
    session = sessionmaker(bind=scoped_engine)()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


def test_check_write_invariants_flags_the_adr_0014_regression(live_postgres_session):
    """Reproduce the exact ADR 0014 incident shape and confirm the probe catches it.

    2ce7981ac5c6_recreate_analytics created bar_prices/factor_loadings_daily
    with a bare ``id BIGINT NOT NULL`` and no unique index -- every write
    failed silently for months because check_critical_tables only checked
    existence. Build that exact broken shape here and confirm the new probe
    flags it, then apply 0106's fix and confirm it goes green.
    """
    db = live_postgres_session
    db.execute(text("""
        CREATE TABLE bar_prices (
            id BIGINT NOT NULL,
            symbol TEXT NOT NULL,
            ts TIMESTAMP NOT NULL,
            open DOUBLE PRECISION NOT NULL,
            high DOUBLE PRECISION NOT NULL,
            low DOUBLE PRECISION NOT NULL,
            close DOUBLE PRECISION NOT NULL,
            volume BIGINT NOT NULL
        )
    """))
    db.execute(text("""
        CREATE TABLE factor_loadings_daily (
            id BIGINT NOT NULL,
            symbol TEXT NOT NULL,
            factor TEXT NOT NULL,
            ts TIMESTAMP NOT NULL,
            loading DOUBLE PRECISION NOT NULL
        )
    """))
    db.execute(text("""
        CREATE TABLE regime_snapshots (
            id BIGINT NOT NULL,
            ts TIMESTAMP NOT NULL
        )
    """))
    db.execute(text("""
        CREATE TABLE provider_health_history (
            id BIGINT NOT NULL,
            ts TIMESTAMP NOT NULL
        )
    """))
    db.commit()

    broken = check_write_invariants(db)
    assert broken["ok"] is False
    assert set(broken["missing_id_default"]) == {
        "bar_prices",
        "factor_loadings_daily",
        "regime_snapshots",
        "provider_health_history",
    }
    assert set(broken["missing_unique_index"]) == {"bar_prices", "factor_loadings_daily"}

    for table in ("bar_prices", "factor_loadings_daily", "regime_snapshots", "provider_health_history"):
        seq = f"{table}_id_seq"
        db.execute(text(f"CREATE SEQUENCE {seq} OWNED BY {table}.id"))
        db.execute(text(f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{seq}')"))
    db.execute(text("CREATE UNIQUE INDEX uq_bar_prices_symbol_ts ON bar_prices (symbol, ts)"))
    db.execute(text(
        "CREATE UNIQUE INDEX uq_factor_loadings_symbol_factor_ts ON factor_loadings_daily (symbol, factor, ts)"
    ))
    db.commit()

    fixed = check_write_invariants(db)
    assert fixed == {"ok": True, "missing_id_default": [], "missing_unique_index": []}
