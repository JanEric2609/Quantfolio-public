"""Bounded concurrent price backfill (audit D2, scalability).

The 15-minute ``refresh_prices`` job used to walk every symbol sequentially.
``backfill_user_prices`` now backfills up to ``BACKFILL_MAX_WORKERS`` symbols at
once, each on its own DB session, while the process-wide provider rate limiters
in ``ProviderRegistry.first_success`` stay shared. Results must be identical to
the sequential walk.
"""
from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from conftest import _create_ormless_tables
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.foundation import price_backfill
from app.foundation.core.db import Base
from app.foundation.models.entities import Holding, Portfolio, User
from app.foundation.price_backfill import _worker_count, backfill_user_prices
from app.foundation.settings import upsert_public_settings

NO_ISIN_WORK = {"resolved": 0, "unresolved": 0, "details": []}


def _seed(db: Session, tickers: list[str]) -> str:
    db.add(User(id="u-conc", username="conc", password_hash="x", role="user", risk_profile="moderate"))
    db.add(Portfolio(id="p-conc", user_id="u-conc", name="P"))
    db.flush()
    for ticker in tickers:
        db.add(Holding(portfolio_id="p-conc", ticker=ticker, name=ticker, quantity=1, avg_buy_price=1))
    db.commit()
    # The passive-core benchmark is added to every user's symbol set; blank it so
    # the symbol counts below are exactly the seeded holdings.
    upsert_public_settings(db, {"passive_core_ticker": ""})
    return "u-conc"


def _engine(tmp_path, name: str = "conc.db"):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _fast(dbapi_connection, _record):  # the DDL of ~300 tables is fsync-bound otherwise
        dbapi_connection.execute("PRAGMA synchronous=OFF")

    Base.metadata.create_all(engine)
    _create_ormless_tables(engine)
    return engine


class _FakeIngester:
    """Stands in for DataIngester: records who ran, and how many ran at once."""

    lock = threading.Lock()
    active = 0
    peak = 0
    sessions: list = []
    threads: set = set()
    fail_on: set = set()

    def __init__(self, db):
        self.db = db

    @classmethod
    def reset(cls):
        cls.active = cls.peak = 0
        cls.sessions, cls.threads, cls.fail_on = [], set(), set()

    def ingest_bar_prices(self, symbol, start_date=None, end_date=None, days=None):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
            cls.sessions.append(self.db)
            cls.threads.add(threading.current_thread().name)
        try:
            time.sleep(0.05)
            if symbol in cls.fail_on:
                raise RuntimeError(f"provider exploded for {symbol}")
            return {"success": True, "symbol": symbol, "rows_inserted": 3, "provider": "fake"}
        finally:
            with cls.lock:
                cls.active -= 1


@pytest.fixture
def fake_ingester(monkeypatch):
    _FakeIngester.reset()
    monkeypatch.setattr(price_backfill, "DataIngester", _FakeIngester)
    monkeypatch.setattr(price_backfill, "resolve_isin_to_ticker", lambda *_a, **_k: NO_ISIN_WORK)
    monkeypatch.setattr(price_backfill, "_last_bar_date", lambda *_a, **_k: None)
    return _FakeIngester


def _db(tmp_path, tickers):
    engine = _engine(tmp_path)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user_id = _seed(db, tickers)
    return db, user_id


def test_symbols_run_concurrently_but_bounded_and_on_separate_sessions(tmp_path, fake_ingester):
    tickers = [f"SYM{i}.DE" for i in range(12)]
    db, user_id = _db(tmp_path, tickers)

    result = backfill_user_prices(db, user_id, days=1825, max_workers=4)

    assert result["total"] == 12 and result["succeeded"] == 12 and result["failed"] == 0
    assert 2 <= fake_ingester.peak <= 4
    # One session per symbol task, none of them the caller's, all distinct.
    assert len(fake_ingester.sessions) == 12
    assert db not in fake_ingester.sessions
    assert len({id(s) for s in fake_ingester.sessions}) == 12
    assert all(name.startswith("price-backfill") for name in fake_ingester.threads)
    assert len(fake_ingester.threads) <= 4


def test_results_match_the_sequential_walk_in_order(tmp_path, fake_ingester):
    tickers = ["ZED.DE", "ALPHA.DE", "MID.DE", "BETA.DE", "OMEGA.DE"]
    db, user_id = _db(tmp_path, tickers)

    sequential = backfill_user_prices(db, user_id, days=1825, max_workers=1)
    assert fake_ingester.peak == 1
    parallel = backfill_user_prices(db, user_id, days=1825, max_workers=4)

    assert parallel == sequential
    assert [r["symbol"] for r in parallel["results"]] == sorted(tickers)


def test_one_symbols_failure_is_contained(tmp_path, fake_ingester):
    db, user_id = _db(tmp_path, ["A.DE", "B.DE", "C.DE", "D.DE"])
    fake_ingester.fail_on = {"C.DE"}

    result = backfill_user_prices(db, user_id, days=1825, max_workers=3)

    assert (result["total"], result["succeeded"], result["failed"]) == (4, 3, 1)
    failed = next(r for r in result["results"] if r["symbol"] == "C.DE")
    assert failed["success"] is False and "provider exploded" in failed["message"]


def test_worker_sessions_are_closed(tmp_path, fake_ingester):
    db, user_id = _db(tmp_path, ["A.DE", "B.DE", "C.DE"])
    closed: list[int] = []
    engine = db.get_bind()

    def factory() -> Session:
        session = sessionmaker(bind=engine, autoflush=False)()
        real_close = session.close
        session.close = lambda: (closed.append(id(session)), real_close())[1]  # type: ignore[method-assign]
        return session

    backfill_user_prices(db, user_id, days=1825, max_workers=2, session_factory=factory)

    assert len(closed) == 3 and len(set(closed)) == 3


def test_worker_count_policy(monkeypatch):
    sqlite_db = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    pg_db = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.delenv("PRICE_BACKFILL_WORKERS", raising=False)

    assert _worker_count(sqlite_db, 50, None) == 1          # one writer at a time on SQLite
    assert _worker_count(sqlite_db, 50, 4) == 4             # explicit always honoured
    assert _worker_count(pg_db, 50, None) == price_backfill.BACKFILL_MAX_WORKERS == 4
    assert _worker_count(pg_db, 2, None) == 2               # never more threads than symbols
    assert _worker_count(pg_db, 50, 0) == 1
    monkeypatch.setenv("PRICE_BACKFILL_WORKERS", "2")
    assert _worker_count(pg_db, 50, None) == 2
    monkeypatch.setenv("PRICE_BACKFILL_WORKERS", "nonsense")
    assert _worker_count(pg_db, 50, None) == 4


def test_default_on_sqlite_stays_sequential(tmp_path, fake_ingester):
    db, user_id = _db(tmp_path, ["A.DE", "B.DE", "C.DE"])
    backfill_user_prices(db, user_id, days=1825)
    assert fake_ingester.peak == 1
    assert fake_ingester.sessions == [db] * 3 or all(s is db for s in fake_ingester.sessions)


# -- end to end: real DataIngester, real rows, concurrent writers ------------


class _FakeRegistry:
    """A provider chain that answers get_price_history with deterministic bars."""

    def get_price_history(self, symbol, start=None, end=None, days=None):
        seed = sum(ord(c) for c in symbol)
        data = [
            {
                "date": (date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                "open": seed + i,
                "high": seed + i + 1,
                "low": seed + i - 1,
                "close": seed + i + 0.5,
                "volume": 100 + i,
                "currency": "EUR",
            }
            for i in range(120)
        ]
        return {"ok": True, "data": data, "provider": "fake"}


def _bars(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT symbol, ts, open, high, low, close, volume, currency, provider FROM bar_prices ORDER BY symbol, ts")
        ).fetchall()


def test_concurrent_backfill_stores_exactly_what_the_sequential_walk_stores(tmp_path):
    tickers = [f"E2E{i}.DE" for i in range(8)]
    with patch("app.foundation.data_backbone.ingest.build_provider_registry", return_value=_FakeRegistry()), \
         patch.object(price_backfill, "resolve_isin_to_ticker", lambda *_a, **_k: NO_ISIN_WORK):
        seq_engine = _engine(tmp_path, "seq.db")
        seq_db = sessionmaker(bind=seq_engine, autoflush=False, autocommit=False)()
        seq_user = _seed(seq_db, tickers)
        seq_result = backfill_user_prices(seq_db, seq_user, days=1825, max_workers=1)

        par_engine = _engine(tmp_path, "par.db")
        par_db = sessionmaker(bind=par_engine, autoflush=False, autocommit=False)()
        par_user = _seed(par_db, tickers)
        par_result = backfill_user_prices(par_db, par_user, days=1825, max_workers=4)

    assert seq_result["succeeded"] == par_result["succeeded"] == 8
    rows = _bars(par_engine)
    assert len(rows) == 8 * 120
    assert rows == _bars(seq_engine)
    assert len({r[0] for r in rows}) == 8


def test_provider_limiters_are_process_wide_so_concurrency_cannot_multiply_quotas():
    from app.foundation.providers.rate_limiter import create_limiters

    first = create_limiters("tiingo")
    second = create_limiters("tiingo")
    assert first and all(a is b for a, b in zip(first, second))
