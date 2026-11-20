"""Check-in 2026-11-19: buy clipped to cash, blank tickers, bar catch-up, quarantine retry, Discover refresh."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import BarSeriesMeta, DiscoverRun, PaperHolding, PaperPortfolio
from conftest import _create_ormless_tables


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    _create_ormless_tables(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _bar(db, symbol: str, day: date, close: float = 10.0) -> None:
    db.execute(
        text(
            "INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider) "
            "VALUES (:s, :ts, :c, :c, :c, :c, 1, 'EUR', 'yfinance')"
        ),
        {"s": symbol, "ts": datetime(day.year, day.month, day.day), "c": close},
    )
    db.commit()


# --- Mandate buys are clipped to the cash, not rejected -----------------------------------------


@pytest.fixture
def cash(monkeypatch):
    box = {"cash": Decimal("13290.44")}
    import app.foundation.paper_cash as paper_cash

    monkeypatch.setattr(paper_cash, "paper_cash_balance", lambda portfolio, db: box["cash"])
    return box


def test_an_oversized_whole_unit_buy_is_clipped_to_what_the_cash_pays(cash):
    from app.decision.llm_portfolio.review import _clip_buy_to_cash

    # Mandate A, 2026-11-17: 176 EUNL.DE at ~127.08 against 13,290.44 EUR.
    clipped = _clip_buy_to_cash(None, None, Decimal("176"), Decimal("127.08"))
    assert clipped == Decimal("104")
    assert clipped * Decimal("127.08") + Decimal("0.99") <= cash["cash"]


def test_a_fractional_buy_is_clipped_to_six_decimals(cash):
    from app.decision.llm_portfolio.review import _clip_buy_to_cash

    clipped = _clip_buy_to_cash(None, None, Decimal("200.5"), Decimal("127.08"))
    assert clipped < Decimal("200.5")
    assert clipped == clipped.quantize(Decimal("0.000001"))
    assert clipped * Decimal("127.08") <= cash["cash"] - 1


def test_a_buy_that_fits_or_cannot_fit_at_all_is_left_alone(cash):
    from app.decision.llm_portfolio.review import _clip_buy_to_cash

    assert _clip_buy_to_cash(None, None, Decimal("10"), Decimal("127.08")) == Decimal("10")
    cash["cash"] = Decimal("50")
    # Not one unit fits: unchanged, so CASH_SUFFICIENCY explains the rejection.
    assert _clip_buy_to_cash(None, None, Decimal("3"), Decimal("127.08")) == Decimal("3")


# --- A blank ticker never reaches the provider chain ------------------------------------------


def test_a_review_tool_called_without_a_ticker_answers_without_fetching(monkeypatch):
    import app.foundation.market as market
    from app.decision.llm_portfolio.review import _run_review_tool

    def boom(*args, **kwargs):
        raise AssertionError("provider chain asked for a blank symbol")

    monkeypatch.setattr(market, "history", boom)
    monkeypatch.setattr(market, "quote", boom)
    monkeypatch.setattr(market, "fundamentals", boom)
    for tool in ("get_history_stats", "get_quote", "get_etf_profile"):
        for args in ({}, {"ticker": ""}, {"ticker": "  "}, {"ticker": None}):
            assert "error" in _run_review_tool(None, tool, args)  # type: ignore[arg-type]


def test_history_of_a_blank_symbol_is_empty(monkeypatch):
    import app.foundation.market as market

    monkeypatch.setattr(market, "_history_rows", lambda *a, **k: pytest.fail("fetched a blank symbol"))
    assert market.history(None, "") == []  # type: ignore[arg-type]
    assert market.history(None, "   ") == []  # type: ignore[arg-type]


# --- The last closed session, by exchange calendar --------------------------------------------


def test_last_closed_session_follows_each_exchange():
    from app.foundation.bar_catch_up import last_closed_session

    thursday_morning = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    assert last_closed_session("SAP.DE", thursday_morning) == date(2026, 11, 18)
    assert last_closed_session("MSFT", thursday_morning) == date(2026, 11, 18)
    # Tokyo closed at 06:30 UTC; three hours later its bar is due.
    assert last_closed_session("6861.T", thursday_morning) == date(2026, 11, 19)
    # Monday morning: Friday for Europe.
    assert last_closed_session("SAP.DE", datetime(2026, 11, 23, 8, 0, tzinfo=UTC)) == date(2026, 11, 20)
    # No calendar (an index): the last weekday.
    assert last_closed_session("^GSPC", datetime(2026, 11, 23, 8, 0, tzinfo=UTC)) == date(2026, 11, 20)


def test_a_us_holiday_is_not_a_missing_bar():
    from app.foundation.bar_catch_up import last_closed_session

    # Thanksgiving 2026-11-26: on Friday morning Wednesday is the newest US session.
    assert last_closed_session("MSFT", datetime(2026, 11, 27, 10, 0, tzinfo=UTC)) == date(2026, 11, 25)
    assert last_closed_session("SAP.DE", datetime(2026, 11, 27, 10, 0, tzinfo=UTC)) == date(2026, 11, 26)


# --- Catch-up of lagging series ---------------------------------------------------------------

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def _paper_holding(db, ticker: str) -> None:
    portfolio = PaperPortfolio(user_id="u1", name="Mandate A")
    db.add(portfolio)
    db.flush()
    db.add(PaperHolding(portfolio_id=portfolio.id, ticker=ticker, name=ticker, quantity=Decimal("1"),
                        avg_buy_price=Decimal("10")))
    db.commit()


def test_lagging_series_are_the_ones_behind_their_exchange():
    from app.foundation.bar_catch_up import lagging_series

    db = _memory_db()
    _bar(db, "SAP.DE", date(2026, 11, 17))  # missed 11-18: lagging
    _bar(db, "EUNL.DE", date(2026, 11, 18))  # current
    _bar(db, "OLD.DE", date(2026, 10, 1))  # not maintained: ignored
    _bar(db, "EIMI.L", date(2026, 10, 1))  # old, but a paper sleeve holds it
    _paper_holding(db, "EIMI.L")
    _bar(db, "SN", date(2026, 11, 17))
    db.add(BarSeriesMeta(symbol="SN", provider="alpaca", quarantine_reason="split_unadjusted", updated_at=NOW))
    db.commit()

    lagging = lagging_series(db, NOW)
    assert set(lagging) == {"SAP.DE", "EIMI.L"}
    assert lagging["SAP.DE"] == (date(2026, 11, 17), date(2026, 11, 18))


def test_catch_up_fetches_from_the_day_before_the_newest_bar_and_counts():
    from app.foundation.bar_catch_up import catch_up_lagging_bars

    db = _memory_db()
    _bar(db, "SAP.DE", date(2026, 11, 17))
    _bar(db, "ENI.MI", date(2026, 11, 17))
    asked: list[tuple[str, str]] = []

    def ingest(symbol: str, start: str) -> dict:
        asked.append((symbol, start))
        if symbol == "SAP.DE":  # the provider has the day now
            _bar(db, symbol, date(2026, 11, 18))
            return {"success": True}
        return {"success": True}  # ENI.MI: the provider still leaves it out

    report = catch_up_lagging_bars(db, now=NOW, ingest=ingest)
    assert sorted(asked) == [("ENI.MI", "2026-11-16"), ("SAP.DE", "2026-11-16")]
    assert (report["lagging"], report["tried"], report["caught_up"], report["still_lagging"]) == (2, 2, 1, 1)
    assert report["examples"] == ["ENI.MI 2026-11-17 < 2026-11-18"]


def test_one_failing_series_does_not_stop_the_rest():
    from app.foundation.bar_catch_up import catch_up_lagging_bars

    db = _memory_db()
    _bar(db, "AAA.DE", date(2026, 11, 17))
    _bar(db, "BBB.DE", date(2026, 11, 17))

    def ingest(symbol: str, start: str) -> dict:
        if symbol == "AAA.DE":
            raise RuntimeError("provider down")
        _bar(db, symbol, date(2026, 11, 18))
        return {"success": True}

    report = catch_up_lagging_bars(db, now=NOW, ingest=ingest)
    assert report["caught_up"] == 1 and report["still_lagging"] == 1


def test_nothing_lagging_asks_nothing():
    from app.foundation.bar_catch_up import catch_up_lagging_bars

    db = _memory_db()
    _bar(db, "SAP.DE", date(2026, 11, 18))
    report = catch_up_lagging_bars(db, now=NOW, ingest=lambda s, d: pytest.fail("fetched a current series"))
    assert report["lagging"] == 0 and report["tried"] == 0


# --- Split quarantines are retried daily ------------------------------------------------------


def test_split_quarantines_are_retried_once_a_day_and_a_clean_repull_clears_them():
    from app.foundation.bar_catch_up import retry_split_quarantines

    db = _memory_db()
    db.add_all([
        BarSeriesMeta(symbol="SN", provider="alpaca", quarantine_reason="split_unadjusted",
                      updated_at=NOW - timedelta(hours=26)),
        BarSeriesMeta(symbol="6861.T", provider="yfinance", quarantine_reason="split_unadjusted",
                      updated_at=NOW - timedelta(hours=26)),
        BarSeriesMeta(symbol="ABVX.PA", provider="yfinance", quarantine_reason="split_unadjusted",
                      updated_at=NOW - timedelta(hours=2)),  # re-pulled this morning
        BarSeriesMeta(symbol="SAP.DE", provider="yfinance", updated_at=NOW - timedelta(days=3)),
    ])
    db.commit()
    asked: list[tuple[str, str | None]] = []

    def repull(symbol: str, provider: str | None) -> dict:
        asked.append((symbol, provider))
        if symbol == "SN":
            pin = db.get(BarSeriesMeta, symbol)
            pin.quarantine_reason = None
            db.commit()
        return {"success": True}

    report = retry_split_quarantines(db, now=NOW, repull=repull)
    assert sorted(asked) == [("6861.T", "yfinance"), ("SN", "alpaca")]
    assert report == {"retried": 2, "cleared": ["SN"], "still": ["6861.T"]}


# --- Discover refresh keeps the shortlist under the advisor's 7 days ---------------------------


def _run(db, status: str, age_days: float) -> None:
    db.add(DiscoverRun(user_id="u1", status=status, created_at=NOW - timedelta(days=age_days)))
    db.commit()


def test_discover_refresh_is_due_at_six_days():
    from app.decision.discover.jobs import discover_refresh_due

    db = _memory_db()
    assert discover_refresh_due(db, "u1", now=NOW)  # never ran
    _run(db, "completed", 5.9)
    assert not discover_refresh_due(db, "u1", now=NOW)
    db2 = _memory_db()
    _run(db2, "completed", 6.1)
    assert discover_refresh_due(db2, "u1", now=NOW)


def test_a_failed_run_since_does_not_make_the_shortlist_fresh():
    from app.decision.discover.jobs import discover_refresh_due

    db = _memory_db()
    _run(db, "completed", 7.0)
    _run(db, "failed", 1.0)
    assert discover_refresh_due(db, "u1", now=NOW)


def test_a_run_in_flight_is_not_doubled():
    from app.decision.discover.jobs import discover_refresh_due

    db = _memory_db()
    _run(db, "completed", 9.0)
    _run(db, "running", 0.1)
    assert not discover_refresh_due(db, "u1", now=NOW)


# --- Edges: date parsing, time budget, zero price, the worker job's verdict ---------------------


def test_as_date_reads_strings_aware_datetimes_and_dates():
    from app.foundation.bar_catch_up import _as_date

    assert _as_date(None) is None
    assert _as_date("2026-11-18 23:30:00") == date(2026, 11, 18)
    assert _as_date(datetime(2026, 11, 19, 0, 30, tzinfo=timezone(timedelta(hours=2)))) == date(2026, 11, 18)
    assert _as_date(date(2026, 11, 18)) == date(2026, 11, 18)
    assert _as_date(12345) is None


def test_catch_up_stops_at_its_time_budget():
    from app.foundation.bar_catch_up import catch_up_lagging_bars

    db = _memory_db()
    _bar(db, "SAP.DE", date(2026, 11, 17))
    report = catch_up_lagging_bars(db, now=NOW, max_seconds=-1, ingest=lambda s, d: pytest.fail("past budget"))
    assert report["stopped_early"] and report["tried"] == 0 and report["lagging"] == 1


def test_a_buy_without_a_price_is_left_alone(cash):
    from app.decision.llm_portfolio.review import _clip_buy_to_cash

    assert _clip_buy_to_cash(None, None, Decimal("176"), Decimal("0")) == Decimal("176")
    assert _clip_buy_to_cash(None, None, Decimal("0"), Decimal("127.08")) == Decimal("0")


@pytest.mark.parametrize(
    ("tried", "still", "status"),
    [(40, 20, "degraded"), (40, 5, "ok"), (8, 8, "ok")],
)
def test_bar_catch_up_job_is_degraded_only_when_a_large_batch_stays_behind(monkeypatch, tried, still, status):
    import app.foundation.bar_catch_up as bcu
    import app.worker as worker

    captured: dict = {}

    def fake_register(name, fn, **kwargs):
        captured["fn"] = fn
        return name

    class _Db:
        closed = False

        def close(self):
            _Db.closed = True

    monkeypatch.setattr(worker, "register_cron_job", fake_register)
    monkeypatch.setattr(worker, "SessionLocal", _Db)
    monkeypatch.setattr(bcu, "catch_up_lagging_bars", lambda db: {"tried": tried, "still_lagging": still})
    monkeypatch.setattr(bcu, "retry_split_quarantines", lambda db: {"retried": 0, "cleared": [], "still": []})

    assert worker.register_bar_catch_up_job(None) == "bar_catch_up"
    result = captured["fn"]()
    assert result["status"] == status and _Db.closed
    assert result["quarantine"] == {"retried": 0, "cleared": [], "still": []}
