"""M0 (discover-pipeline audit fix): benchmark ETFs must be persisted during warm-up.

Audit refs: docs/archive/audits/2026-08-discover-maths/AUDIT_REPORT.md F10-F12 — VGK was
absent from bar_prices, so stage_backtest_vs_benchmark live-fetched benchmarks on
every run and candidate anchors silently inherited the live provider's vintage.
These tests pin the new contract of ``_preingest_candidates``:

* candidates + the three benchmark ETFs are requested (ordered-unique union),
* a per-run summary ``{requested, ok, failed}`` is returned,
* benchmark ingest failures never abort candidate processing,
* re-running is idempotent against the shared bar_prices store.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.decision.discover import pipeline

# Single source of truth for the benchmark tickers — never duplicate literals.
BENCHMARKS = [
    pipeline._BENCHMARK_EUROPE,
    pipeline._BENCHMARK_US,
    pipeline._BENCHMARK_GLOBAL_ETF,
    pipeline._BENCHMARK_JAPAN,
]


def _test_db():
    """Fresh in-memory SQLite engine + session factory with the bar_prices shim.

    The engine is returned too because ``_preingest_one`` opens its own session
    via ``pipeline.SessionLocal``; the end-to-end test rebinds that symbol to
    this factory so the worker threads write into THIS database (StaticPool
    shares one connection across threads).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # bar_prices is a raw-SQL table accessed outside the ORM (DataIngester);
    # shim it explicitly (pattern per tests/test_alphacrafter_panel.py:28-39).
    # UNIQUE(symbol, ts) mirrors migration 0075 so ON CONFLICT upserts resolve.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS bar_prices (
                    symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,
                    close REAL, volume REAL, currency TEXT, provider TEXT,
                    UNIQUE (symbol, ts)
                )
                """
            )
        )
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _recorder(requested: list[str], fail_symbols: set[str] | None = None) -> type:
    """Build a fake DataIngester class that records requested symbols.

    We patch ``pipeline.DataIngester`` (not the method) because the real
    constructor runs build_provider_registry(), which needs a fully migrated
    DB — irrelevant to what these tests exercise.
    """
    fails = fail_symbols or set()

    class _FakeIngester:
        def __init__(self, db):
            self.db = db

        def ingest_bar_prices(self, symbol, days=None):
            requested.append(symbol)
            if symbol in fails:
                return {"success": False, "symbol": symbol, "message": "provider down"}
            return {"success": True, "symbol": symbol, "rows_inserted": 5}

    return _FakeIngester


def test_preingest_requests_benchmarks_and_candidates(monkeypatch):
    """Warm-up must request candidates AND the three benchmark ETFs."""
    requested: list[str] = []
    monkeypatch.setattr(pipeline, "DataIngester", _recorder(requested))

    summary = pipeline._preingest_candidates(["BBVA.MC"])

    assert set(requested) >= {"BBVA.MC", *BENCHMARKS}
    # Candidates keep precedence; benchmarks are appended after them.
    assert requested[0] == "BBVA.MC"
    assert summary["requested"] == len(requested)
    assert summary["ok"] == len(requested)
    assert summary["failed"] == []


def test_benchmark_symbols_deduped_against_candidates(monkeypatch):
    """A candidate already equal to a benchmark must not be ingested twice."""
    requested: list[str] = []
    monkeypatch.setattr(pipeline, "DataIngester", _recorder(requested))

    pipeline._preingest_candidates([
        pipeline._BENCHMARK_US,
        pipeline._BENCHMARK_US.lower(),
    ])

    assert requested == [
        pipeline._BENCHMARK_US,
        pipeline._BENCHMARK_EUROPE,
        pipeline._BENCHMARK_GLOBAL_ETF,
        pipeline._BENCHMARK_JAPAN,
    ]


def test_end_to_end_rows_in_shared_engine_idempotent(monkeypatch):
    """Workers write into the caller's engine; a second run adds zero rows."""
    engine, factory = _test_db()
    monkeypatch.setattr(pipeline, "SessionLocal", factory)

    requested: list[str] = []
    # StaticPool hands every session the SAME sqlite3 connection, and pysqlite
    # forbids concurrent use of one connection across threads (prod uses
    # PostgreSQL). Serialise the fake's writes so workers don't interleave.
    write_lock = threading.Lock()

    class _RowWritingIngester:
        """Fake that mimics DataIngester's idempotent upsert path."""

        def __init__(self, db):
            self.db = db

        def ingest_bar_prices(self, symbol, days=None):
            requested.append(symbol)
            base = datetime(2024, 1, 1, tzinfo=UTC)
            params = [
                {
                    "symbol": symbol,
                    "ts": base + timedelta(days=i),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "volume": 1000,
                    "currency": "USD",
                    "provider": "test",
                }
                for i in range(5)
            ]
            with write_lock:
                self.db.execute(
                    text(
                        """
                        INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
                        VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
                        ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
                        """
                    ),
                    params,
                )
                self.db.commit()
            return {"success": True, "symbol": symbol, "rows_inserted": len(params)}

    monkeypatch.setattr(pipeline, "DataIngester", _RowWritingIngester)

    db = factory()
    try:
        pipeline._preingest_candidates(["BBVA.MC"])
        count_after_first = db.execute(text("SELECT COUNT(*) FROM bar_prices")).scalar_one()
        persisted = {
            row[0] for row in db.execute(text("SELECT DISTINCT symbol FROM bar_prices"))
        }

        pipeline._preingest_candidates(["BBVA.MC"])
        count_after_second = db.execute(text("SELECT COUNT(*) FROM bar_prices")).scalar_one()

        assert persisted >= {"BBVA.MC", *BENCHMARKS}
        assert count_after_second == count_after_first  # zero duplicates on re-run
    finally:
        db.close()


def test_benchmark_ingest_failure_does_not_block_candidates(monkeypatch):
    """VGK failing must neither raise nor drop any candidate from the request set."""
    requested: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "DataIngester",
        _recorder(requested, fail_symbols={pipeline._BENCHMARK_EUROPE}),
    )

    summary = pipeline._preingest_candidates(["BBVA.MC"])  # must not raise

    assert {"BBVA.MC", pipeline._BENCHMARK_US, pipeline._BENCHMARK_GLOBAL_ETF} <= set(requested)
    assert summary["failed"] == [pipeline._BENCHMARK_EUROPE]
    assert summary["ok"] == len(requested) - 1
    assert summary["requested"] == len(requested)
