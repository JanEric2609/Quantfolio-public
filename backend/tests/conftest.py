"""Global test configuration: force in-memory DB before any app import."""
import os
import sys

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("APP_ENV", "local")
# A fixed first-run setup token: no test may generate one (that writes a file into
# the data dir). Tests of the token itself clear this and point DATA_DIR at tmp_path.
os.environ.setdefault("SETUP_TOKEN", "test-setup-token")
# No shared Redis in tests: the provider rate limiters and the passkey challenge
# store use Redis only when REDIS_URL is explicitly configured, and a developer's
# own Redis must never make a test depend on cross-process state.
os.environ["REDIS_URL"] = ""

# Sanity check — if app.foundation.core.db was already imported, ensure it uses :memory:
if "app.foundation.core.db" in sys.modules:
    from app.foundation.core.db import engine

    if getattr(engine.url, "database", None) not in (":memory:", None):
        raise RuntimeError(
            f"Tests must not run against a file DB (got {engine.url}). "
            "Ensure conftest.py sets DATABASE_URL before app import."
        )

from app.foundation.core.config import get_settings

get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Shared in-memory SQLite session factory.
#
# Historically every test file redefined an identical `_memory_db()` helper.
# This is the single canonical implementation; test modules import it via
# `from conftest import _memory_db`. Each call returns a *fresh* isolated
# in-memory database, so tests that need several independent DBs can still call
# it multiple times. Files needing extra schema setup (e.g. TimescaleDB
# hypertable shims) or a session *factory* keep their own local helper.
# ---------------------------------------------------------------------------
import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.foundation.core.db import Base  # noqa: E402
# Import all models so Base.metadata knows about them before create_all
from app.foundation.models import entities  # noqa: E402


# ---------------------------------------------------------------------------
# Tables that have no ORM model
# ---------------------------------------------------------------------------
#
# Four TimescaleDB hypertables are created by raw migration DDL and reached only
# through raw SQL, so ``Base.metadata.create_all()`` does not know about them and
# tests must create them by hand.
#
# This DDL mirrors ``alembic/versions/0015_hypertables.py`` column for column,
# including every NOT NULL and server default, plus the business-key unique
# indexes from ``0075_bar_prices_unique_index.py`` that the ``ON CONFLICT``
# upserts in ``foundation/data_backbone/`` depend on.
#
# Keeping it faithful is load-bearing, not cosmetic. The previous hand-written
# version had ``factor_loadings_daily`` wide (one column per factor) where the
# real schema is long (``symbol, factor, ts, loading``), and named
# ``provider_health_history``'s columns ``status``/``error_message`` where the
# real schema has ``capability``/``ok``/``message``. Both write paths swallow
# their exceptions, so against that fixture they were silent no-ops: the suite
# stayed green while the code under test never actually wrote a row.
#
# ``id`` is ``INTEGER PRIMARY KEY AUTOINCREMENT`` here rather than the composite
# ``(id, ts)`` PK the migration declares. Production attaches an identity
# sequence to ``id`` in ``0073_hypertable_id_defaults.py`` (PostgreSQL-only,
# because a composite PK gets no autoincrement from SQLAlchemy); AUTOINCREMENT is
# SQLite's equivalent, and it is what lets the INSERTs that omit ``id`` -- all of
# them -- behave here the way they behave in production.
_ORMLESS_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS bar_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol VARCHAR(32) NOT NULL,
        ts TIMESTAMP NOT NULL,
        open FLOAT NOT NULL,
        high FLOAT NOT NULL,
        low FLOAT NOT NULL,
        close FLOAT NOT NULL,
        volume BIGINT NOT NULL,
        currency VARCHAR(3) NOT NULL DEFAULT 'USD',
        provider VARCHAR(40) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_loadings_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol VARCHAR(32) NOT NULL,
        factor VARCHAR(80) NOT NULL,
        ts TIMESTAMP NOT NULL,
        loading FLOAT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regime_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TIMESTAMP NOT NULL,
        label VARCHAR(80) NOT NULL,
        score FLOAT NOT NULL,
        source VARCHAR(40) NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_health_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider VARCHAR(40) NOT NULL,
        ts TIMESTAMP NOT NULL,
        capability VARCHAR(80) NOT NULL,
        ok BOOLEAN NOT NULL,
        latency_ms FLOAT,
        message TEXT
    )
    """,
)

_ORMLESS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_bar_prices_symbol_ts ON bar_prices (symbol, ts)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bar_prices_symbol_ts ON bar_prices (symbol, ts)",
    "CREATE INDEX IF NOT EXISTS ix_factor_loadings_daily_symbol_factor_ts "
    "ON factor_loadings_daily (symbol, factor, ts)",
    "CREATE INDEX IF NOT EXISTS ix_factor_loadings_daily_factor_ts "
    "ON factor_loadings_daily (factor, ts)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_loadings_symbol_factor_ts "
    "ON factor_loadings_daily (symbol, factor, ts)",
    "CREATE INDEX IF NOT EXISTS ix_regime_snapshots_label_ts ON regime_snapshots (label, ts)",
    "CREATE INDEX IF NOT EXISTS ix_provider_health_history_provider_ts "
    "ON provider_health_history (provider, ts)",
)


def _create_ormless_tables(engine) -> None:
    """Create the four hypertables that have no ORM model, matching production."""
    with engine.begin() as conn:
        for ddl in _ORMLESS_TABLE_DDL:
            conn.execute(text(ddl))
        for ddl in _ORMLESS_INDEX_DDL:
            conn.execute(text(ddl))


def _memory_db():
    """Return a new session bound to a fresh in-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    _create_ormless_tables(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def memory_db():
    """Pytest-fixture form of :func:`_memory_db`; closes the session on teardown."""
    db = _memory_db()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _no_live_justetf(monkeypatch, tmp_path):
    """Keep tests off justETF and off the machine-wide ETF-universe cache.

    Persisting a DKB snapshot enriches assets through
    ``EtfUniverseProvider.lookup_by_isin``, which fetched the live universe
    whenever ``backend/.cache/etf_universe`` was older than a day and rewrote
    it. A parallel worker reading that file mid-write saw no universe, so
    ``test_gain_harvest`` intermittently classified EUNL as "other". A test
    that needs universe records stubs them.
    """
    from app.foundation import etf_universe

    def _offline() -> list:
        raise RuntimeError("tests must not fetch the live justETF universe")

    monkeypatch.setattr(etf_universe.EtfUniverseProvider, "_fetch_from_justetf", staticmethod(_offline))
    monkeypatch.setattr(etf_universe, "_CACHE_DIR", str(tmp_path / "etf_universe_cache"))


@pytest.fixture(autouse=True)
def _no_live_analyst_estimates(monkeypatch):
    """Keep Discover's estimate stage off Yahoo: without a fresh IBES row it
    falls back to a live EPS-trend fetch. A test that needs one passes
    ``fetch=`` or stubs this."""
    from app.foundation.data_backbone import analyst_estimates

    monkeypatch.setattr(analyst_estimates, "fetch_eps_trend", lambda _symbol: None)


@pytest.fixture(autouse=True)
def _fresh_settings_cache():
    """``get_public_settings`` is cached per engine for a few seconds; start and
    end every test with a cold cache so no test can read another's settings."""
    from app.foundation.settings import invalidate_settings_cache

    invalidate_settings_cache()
    yield
    invalidate_settings_cache()
