"""Check-in 2026-11-17: index-member ISINs, point-in-time panel fundamentals,
a miner timeout that fires, one Finnhub connection, the price refresh count."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import Fundamental, IndexUniverseMember


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS bar_prices (symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,"
            " close REAL, volume REAL, currency TEXT, provider TEXT)"
        ))
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# --- ISIN from the index members --------------------------------------------------------------------


def _member(db, isin: str, symbol: str, ok: bool = True) -> None:
    db.add(IndexUniverseMember(index_key="msci_world", isin=isin, name=symbol, symbol=symbol, symbol_ok=ok))
    db.commit()


def test_an_index_member_gives_the_isin_yahoo_gets_wrong():
    from app.foundation.asset_lookup import local_isin_for_ticker

    db = _memory_db()
    _member(db, "IT0003132476", "ENI.MI")
    _member(db, "FR0000120271", "TTE.PA")
    assert local_isin_for_ticker(db, "eni.mi") == "IT0003132476"
    assert local_isin_for_ticker(db, "TTE.PA") == "FR0000120271"


def test_an_unconfirmed_member_symbol_is_not_used():
    from app.foundation.asset_lookup import local_isin_for_ticker

    db = _memory_db()
    _member(db, "SG1L01001701", "D05.SI", ok=False)
    assert local_isin_for_ticker(db, "D05.SI") is None


def test_the_backfill_fills_an_isin_from_the_index_members():
    from app.decision.paper_portfolio import backfill_holding_isins
    from app.foundation.models.entities import PaperHolding, PaperPortfolio, User

    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    p = PaperPortfolio(user_id=user.id, name="Advisor Loop")
    db.add(p)
    db.commit()
    db.add(PaperHolding(portfolio_id=p.id, ticker="ENI.MI", name="Eni", quantity=1, avg_buy_price=14, currency="EUR"))
    db.commit()
    _member(db, "IT0003132476", "ENI.MI")
    backfill_holding_isins(db, allow_network=False)
    assert db.query(PaperHolding).one().isin == "IT0003132476"


# --- panel fundamentals ----------------------------------------------------------------------------------


def _seed_bars(db, symbols, days=20):
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for i, sym in enumerate(symbols):
        for d in range(days):
            db.execute(
                text("INSERT INTO bar_prices VALUES (:s, :ts, 1, 1, 1, :c, 1000, 'EUR', 'test')"),
                {"s": sym, "ts": base + timedelta(days=d), "c": 100.0 + i + d},
            )
    db.commit()


class _Registry:
    def __init__(self, data):
        self.data = data
        self.calls: list[str] = []

    def get_fundamentals(self, symbol):
        self.calls.append(symbol)
        if symbol in self.data:
            return {"ok": True, "data": self.data[symbol]}
        return {"ok": False}


def _panel(db, symbols, registry):
    from app.lab.alphacrafter.panel import build_panel

    return build_panel(
        db, symbols, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC), registry=registry,
        include_wrds_factors=False, include_ibes_estimates=False, include_insider_signals=False,
    )


def test_todays_fundamentals_never_reach_earlier_rows():
    db = _memory_db()
    _seed_bars(db, ["AAA"])
    panel = _panel(db, ["AAA"], _Registry({"AAA": {"pe_ratio": 12.0, "market_cap": 5e9, "roe": 0.2}}))
    for field, value in (("pe_ratio", 12.0), ("market_cap", 5e9), ("roe", 0.2)):
        assert panel[field]["AAA"].iloc[-1] == value
        assert panel[field]["AAA"].iloc[:-1].isna().all()


def test_a_size_factor_has_no_history_to_score():
    """With no past values the IC of log(market_cap) has nothing to measure."""
    from app.lab.alphacrafter.miner import evaluate_candidate

    db = _memory_db()
    _seed_bars(db, ["AAA", "BBB", "CCC"], days=40)
    registry = _Registry({s: {"market_cap": 1e9 * (i + 1)} for i, s in enumerate(["AAA", "BBB", "CCC"])})
    panel = _panel(db, ["AAA", "BBB", "CCC"], registry)
    cand = evaluate_candidate(panel, name="size_log_mc", formula="log(market_cap)", source="quant_factors",
                              dsl=None, horizon=5, min_ic=0.02, min_icir=0.3)
    assert cand.valid is False
    assert cand.ic_series == []


def test_a_fresh_cache_row_saves_the_provider_call():
    db = _memory_db()
    _seed_bars(db, ["AAA", "BBB", "CCC"])
    db.add(Fundamental(ticker="AAA", data_json=json.dumps({"pe_ratio": 9.0}), source="finnhub",
                       fetched_at=datetime.now(UTC) - timedelta(days=2)))
    db.add(Fundamental(ticker="BBB", data_json=json.dumps({"pe_ratio": 99.0}), source="finnhub",
                       fetched_at=datetime.now(UTC) - timedelta(days=30)))
    db.add(Fundamental(ticker="CCC", data_json=json.dumps({"market_cap": 7e9, "name": "C"}), source="universe_screen",
                       fetched_at=datetime.now(UTC)))
    db.commit()
    registry = _Registry({"BBB": {"pe_ratio": 20.0}, "CCC": {"pe_ratio": 30.0}})
    panel = _panel(db, ["AAA", "BBB", "CCC"], registry)
    assert sorted(registry.calls) == ["BBB", "CCC"]  # stale rows and screen stubs are fetched
    assert panel["pe_ratio"]["AAA"].iloc[-1] == 9.0
    assert panel["pe_ratio"]["BBB"].iloc[-1] == 20.0
    assert panel["pe_ratio"]["CCC"].iloc[-1] == 30.0


# --- the miner's timeout ---------------------------------------------------------------------------------------


def test_a_blocking_stage_times_out_on_time():
    from app.lab.alphacrafter.orchestrator import _in_stage_thread, _run_coro_in_thread_with_session, _safe_stage

    db = _memory_db()
    seen: dict[str, object] = {}

    async def blocking_miner(session, universe, *, lookback_years):
        seen["own_session"] = session is not db
        seen["universe"] = universe
        time.sleep(1.5)  # synchronous work, like the panel build
        return ["late"]

    async def run():
        start = time.monotonic()
        result, summary = await _safe_stage(
            db, None, "miner",
            _in_stage_thread(_run_coro_in_thread_with_session, db.get_bind(), blocking_miner, ["AAA"],
                             lookback_years=5.0),
            "stages.miner", timeout_s=0.2,
        )
        return result, summary, time.monotonic() - start

    start = time.monotonic()
    result, summary, elapsed = asyncio.run(run())
    assert result is None and summary["status"] == "error"
    assert elapsed < 1.0
    # asyncio.run does not wait for the late thread either: the cron job ends on time.
    assert time.monotonic() - start < 1.2
    assert seen == {"own_session": True, "universe": ["AAA"]}


def test_a_stage_in_a_thread_returns_its_result():
    from app.lab.alphacrafter.orchestrator import _in_stage_thread, _run_coro_in_thread_with_session, _safe_stage

    db = _memory_db()

    async def miner(session, universe):
        return [s.lower() for s in universe]

    async def run():
        return await _safe_stage(
            db, None, "miner",
            _in_stage_thread(_run_coro_in_thread_with_session, db.get_bind(), miner, ["AAA", "BBB"]),
            "stages.miner",
        )

    result, summary = asyncio.run(run())
    assert result == ["aaa", "bbb"] and summary["status"] == "ok"


# --- one Finnhub connection ---------------------------------------------------------------------------------------


def test_finnhub_reuses_one_client_across_requests():
    from app.foundation.providers.finnhub_provider import FinnhubProvider

    response = MagicMock(status_code=200)
    response.json.return_value = {"metric": {"peTTM": 20.0}, "marketCapitalization": 1000.0}
    client = MagicMock()
    client.get.return_value = response
    provider = FinnhubProvider(api_key="k")
    with patch("app.foundation.providers.finnhub_provider.httpx.Client", return_value=client) as cls:
        assert provider.get_fundamentals("KO")["ok"] is True
        assert provider.get_fundamentals("PG")["ok"] is True
    assert cls.call_count == 1
    assert client.get.call_count == 4


def test_a_dropped_finnhub_provider_closes_its_client():
    from app.foundation.providers.finnhub_provider import FinnhubProvider

    client = MagicMock()
    provider = FinnhubProvider(api_key="k")
    provider._client = client
    provider.__del__()
    client.close.assert_called_once()
    FinnhubProvider(api_key="k").__del__()  # never used: nothing to close


def test_finnhub_error_still_hides_the_token():
    from app.foundation.providers.finnhub_provider import FinnhubProvider

    client = MagicMock()
    client.get.return_value = MagicMock(status_code=429)
    provider = FinnhubProvider(api_key="SECRET-TOKEN")
    with patch("app.foundation.providers.finnhub_provider.httpx.Client", return_value=client):
        out = provider.get_fundamentals("KO")
    assert out["ok"] is False
    assert "SECRET-TOKEN" not in json.dumps(out, default=str)


# --- the price refresh count ------------------------------------------------------------------------------------


@pytest.fixture
def user_with_holdings():
    from app.foundation.models.entities import Holding, Portfolio, User

    db = _memory_db()
    user = User(username="u", password_hash="x")
    db.add(user)
    db.commit()
    portfolio = Portfolio(user_id=user.id, name="Main")
    db.add(portfolio)
    db.commit()
    for ticker in ("OK.DE", "OPEN.DE", "BAD.DE"):
        db.add(Holding(portfolio_id=portfolio.id, ticker=ticker, name=ticker, quantity=1, avg_buy_price=1))
    db.commit()
    return db, user.id


def test_a_session_still_open_is_not_a_failure(user_with_holdings):
    from app.foundation.price_backfill import backfill_user_prices

    db, user_id = user_with_holdings

    def ingest(**kwargs):
        sym = kwargs["symbol"]
        if sym == "OK.DE":
            return {"success": True, "symbol": sym}
        if sym == "BAD.DE":
            return {"success": False, "symbol": sym, "message": "No data returned"}
        return {"success": False, "symbol": sym, "message": "only a session still open", "pending_close": True}

    with patch("app.foundation.price_backfill.DataIngester") as ingester:
        ingester.return_value.ingest_bar_prices.side_effect = ingest
        result = backfill_user_prices(db, user_id, days=30, max_workers=1)
    # OPEN.DE and the passive core (EUNL.DE) only have today's open session.
    assert result["succeeded"] == 1
    assert result["pending_close"] == 2
    assert result["failed"] == 1


def test_ingest_marks_an_open_session_only_fetch():
    from app.foundation.data_backbone.ingest import DataIngester

    db = _memory_db()
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    ingester = DataIngester(db)
    ingester.registry = MagicMock()
    ingester.registry.get_price_history.return_value = {
        "ok": True, "provider": "yfinance",
        "data": [{"date": today.isoformat(), "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.2, "volume": 5.0}],
    }
    with patch("app.foundation.data_backbone.bar_guard.session_incomplete", return_value=True):
        out = ingester.ingest_bar_prices("OPEN.DE", start_date=today.date().isoformat())
    assert out["success"] is False and out["pending_close"] is True
