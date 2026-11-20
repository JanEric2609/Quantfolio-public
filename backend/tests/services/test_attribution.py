"""Tests for attribution services (Phase 3)."""

import pytest
from conftest import _memory_db

from app.lab.attribution import brinson_fachler, top_contributors


def test_brinson_fachler_sums_to_active_return():
    """Brinson decomposition should sum to total active return."""
    # Synthetic 2-sector portfolio
    portfolio_holdings = [
        {"isin": "TECH01", "ticker": "TECH", "name": "Tech Fund", "value": 600},  # 60% weight
        {"isin": "BANK01", "ticker": "BANK", "name": "Bank Fund", "value": 400},  # 40% weight
    ]

    benchmark_holdings = [
        {"isin": "TECH01", "ticker": "TECH", "name": "Tech Fund", "value": 500},  # 50% weight
        {"isin": "BANK01", "ticker": "BANK", "name": "Bank Fund", "value": 500},  # 50% weight
    ]

    portfolio_returns = {
        "TECH01": 0.10,  # Tech: 10%
        "BANK01": 0.05,  # Bank: 5%
    }

    benchmark_returns = {
        "TECH01": 0.08,  # Tech: 8%
        "BANK01": 0.04,  # Bank: 4%
    }

    result = brinson_fachler(
        portfolio_holdings, benchmark_holdings, portfolio_returns, benchmark_returns
    )

    # Portfolio return: 0.6 * 0.10 + 0.4 * 0.05 = 0.08 (8%)
    # Benchmark return: 0.5 * 0.08 + 0.5 * 0.04 = 0.06 (6%)
    # Active return: 8% - 6% = 2%

    expected_active_return = 0.02
    actual_active_return = result.total_active_return

    # Check sum within tolerance (1e-6)
    assert abs(actual_active_return - expected_active_return) < 1e-6, (
        f"Active return {actual_active_return} != {expected_active_return}"
    )

    # Verify individual effects sum to total
    total_effects = (
        result.allocation_effect + result.selection_effect + result.interaction_effect
    )
    assert abs(total_effects - actual_active_return) < 1e-6


def test_top_contributors_positive():
    """Top contributors extraction should work correctly."""
    from app.lab.attribution import SecurityAttribution

    securities = [
        SecurityAttribution(
            isin="SEC1",
            ticker="T1",
            name="Security 1",
            portfolio_weight=0.5,
            benchmark_weight=0.3,
            portfolio_return=0.10,
            benchmark_return=0.08,
            allocation_effect=0.01,
            selection_effect=0.01,
            interaction_effect=0.001,
        ),
        SecurityAttribution(
            isin="SEC2",
            ticker="T2",
            name="Security 2",
            portfolio_weight=0.3,
            benchmark_weight=0.4,
            portfolio_return=0.05,
            benchmark_return=0.06,
            allocation_effect=-0.01,
            selection_effect=-0.005,
            interaction_effect=0.0,
        ),
        SecurityAttribution(
            isin="SEC3",
            ticker="T3",
            name="Security 3",
            portfolio_weight=0.2,
            benchmark_weight=0.3,
            portfolio_return=0.08,
            benchmark_return=0.07,
            allocation_effect=-0.001,
            selection_effect=0.002,
            interaction_effect=-0.0001,
        ),
    ]

    top_pos = top_contributors(securities, n=2, direction="positive")
    assert len(top_pos) == 2
    assert top_pos[0].isin == "SEC1"  # Largest positive contributor
    assert top_pos[0].total_contribution > 0

    top_neg = top_contributors(securities, n=1, direction="negative")
    assert len(top_neg) == 1
    assert top_neg[0].isin == "SEC2"  # Largest negative contributor
    assert top_neg[0].total_contribution < 0


# ---------------------------------------------------------------------------
# API-level: run_brinson_attribution must not crash on a holding with unknown
# (None) avg_buy_price. This file otherwise only tests the pure
# app.lab.attribution functions, so the DB/portfolio fixtures below are
# added locally rather than assuming a pre-existing helper in this module.
# ---------------------------------------------------------------------------

def test_run_brinson_attribution_skips_holding_with_unknown_avg_buy_price(monkeypatch):
    """A holding with avg_buy_price=None (unknown DKB cost basis) must be
    excluded from the attribution run rather than crashing the endpoint with
    ``TypeError: float() argument must be a string or a number, not 'NoneType'``
    (raised at the ``value = float(h.quantity) * float(h.avg_buy_price)``
    price-proxy fallback when no bar data is available for the holding).
    """
    import pandas as pd
    from datetime import date as date_cls, timedelta
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.foundation.core.db import Base
    from app.foundation.models.entities import Holding, Portfolio, User
    from app.interface.api import attribution as attribution_api

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    user = User(id=str(uuid4()), username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    portfolio = Portfolio(id=str(uuid4()), user_id=user.id, name="Main")
    db.add(portfolio)
    db.commit()

    unknown_isin = "US0000000UNKN"
    known_isin = "US1111111111"
    db.add(
        Holding(
            id=str(uuid4()), portfolio_id=portfolio.id, isin=unknown_isin, ticker="NONE1",
            name="Unknown Cost Basis", asset_type="stock", quantity=Decimal("10"),
            avg_buy_price=None, source="dkb_sync",
        )
    )
    db.add(
        Holding(
            id=str(uuid4()), portfolio_id=portfolio.id, isin=known_isin, ticker="HASPRICE",
            name="Known Cost Basis", asset_type="stock", quantity=Decimal("5"),
            avg_buy_price=Decimal("50"), source="manual",
        )
    )
    db.commit()

    class _FakeBarStore:
        """No bar data for portfolio holdings (forces the avg_buy_price proxy
        path); valid bars for the benchmark so the endpoint can complete."""

        def __init__(self, _db):
            pass

        def get_bars(self, symbol, start=None, end=None, as_of=None):
            if symbol == "BENCH":
                return pd.DataFrame({"close": [100.0, 110.0]})
            return None

    monkeypatch.setattr(attribution_api, "BarStore", _FakeBarStore)

    request = attribution_api.BrinsonRequest(
        portfolio_id=portfolio.id,
        benchmark_ticker="BENCH",
        date_from=date_cls.today() - timedelta(days=30),
        date_to=date_cls.today(),
    )

    result = attribution_api.run_brinson_attribution(request, db=db, user=user)

    isins_in_result = {s["isin"] for s in result["attribution"]["securities"]}
    assert unknown_isin not in isins_in_result, (
        "Holding with avg_buy_price=None must be excluded, not crash or "
        "silently contribute a value"
    )
    assert known_isin in isins_in_result


# ---------------------------------------------------------------------------
# Bug 1: date_from/date_to were accepted by factor_attribution() but never
# passed through to quant_factors.get_ff3_returns(), so every call pulled
# the FULL Fama-French history instead of the requested window.
# ---------------------------------------------------------------------------


def test_factor_attribution_passes_parsed_dates_to_get_ff3_returns(monkeypatch):
    """factor_attribution() must parse date_from/date_to and forward them as
    ``start``/``end`` (as ``date`` objects) to ``get_ff3_returns``."""
    from datetime import date as date_cls

    from app.foundation import quant_factors
    from app.lab.attribution.factor_attrib import factor_attribution

    captured: dict = {}

    def _fake_get_ff3_returns(start=None, end=None):
        captured["start"] = start
        captured["end"] = end
        return {"status": "completed", "factors": {}}

    monkeypatch.setattr(quant_factors, "get_ff3_returns", _fake_get_ff3_returns)

    factor_attribution(
        portfolio_returns=[0.01, 0.02],
        benchmark_returns=[0.005, 0.015],
        date_from="2024-01-01",
        date_to="2024-06-30",
    )

    assert captured["start"] == date_cls(2024, 1, 1)
    assert captured["end"] == date_cls(2024, 6, 30)


# ---------------------------------------------------------------------------
# Bug 2: the factor-attribution endpoint aligned per-holding and benchmark
# return series by list position, not by calendar date. Holdings/benchmark
# with different gap patterns must be inner-joined on date before averaging.
# ---------------------------------------------------------------------------


def test_run_factor_attribution_aligns_returns_by_date_not_position(monkeypatch):
    """Two holdings with different date gaps, plus a benchmark with yet
    another gap pattern, must only contribute dates where all three have
    data — not the first N observations by list position."""
    import pandas as pd
    from datetime import date as date_cls
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.foundation.core.db import Base
    from app.foundation.models.entities import Holding, Portfolio, User
    from app.interface.api import attribution as attribution_api
    from app.lab.attribution.factor_attrib import FactorAttributionResult

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    user = User(id=str(uuid4()), username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    portfolio = Portfolio(id=str(uuid4()), user_id=user.id, name="Main")
    db.add(portfolio)
    db.commit()

    db.add(
        Holding(
            id=str(uuid4()), portfolio_id=portfolio.id, isin="AAPL_ISIN", ticker="AAPL",
            name="Apple", asset_type="stock", quantity="1", avg_buy_price="100", source="manual",
        )
    )
    db.add(
        Holding(
            id=str(uuid4()), portfolio_id=portfolio.id, isin="MSFT_ISIN", ticker="MSFT",
            name="Microsoft", asset_type="stock", quantity="1", avg_buy_price="200", source="manual",
        )
    )
    db.commit()

    # AAPL has bars for every day 1/1-1/5. MSFT is missing 1/3. The
    # benchmark is missing 1/2. Only 1/4 and 1/5 have both a portfolio-wide
    # (AAPL ∩ MSFT) return AND a benchmark return.
    def _bars(dates: list[str], closes: list[float]) -> pd.DataFrame:
        return pd.DataFrame({"ts": pd.to_datetime(dates), "close": closes})

    aapl_bars = _bars(
        ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
        [100.0, 101.0, 102.0, 103.0, 104.0],
    )
    msft_bars = _bars(
        ["2024-01-01", "2024-01-02", "2024-01-04", "2024-01-05"],
        [200.0, 202.0, 204.0, 206.0],
    )
    bench_bars = _bars(
        ["2024-01-01", "2024-01-03", "2024-01-04", "2024-01-05"],
        [50.0, 51.0, 52.0, 53.0],
    )

    class _FakeBarStore:
        def __init__(self, _db):
            pass

        def get_bars(self, symbol, start=None, end=None, as_of=None):
            return {"AAPL": aapl_bars, "MSFT": msft_bars, "BENCH": bench_bars}.get(symbol)

    monkeypatch.setattr(attribution_api, "BarStore", _FakeBarStore)

    captured: dict = {}

    def _fake_factor_attribution(
        portfolio_returns, benchmark_returns, date_from, date_to, factors=None, dates=None
    ):
        captured["portfolio_returns"] = portfolio_returns
        captured["benchmark_returns"] = benchmark_returns
        captured["dates"] = dates
        return FactorAttributionResult(factors=[], residual=0.0, r_squared=0.0, total_attribution=0.0)

    monkeypatch.setattr(attribution_api, "factor_attribution", _fake_factor_attribution)

    request = attribution_api.FactorAttributionRequest(
        portfolio_id=portfolio.id,
        benchmark_ticker="BENCH",
        date_from=date_cls(2024, 1, 1),
        date_to=date_cls(2024, 1, 5),
    )

    attribution_api.run_factor_attribution(request, db=db, user=user)

    # A position-based alignment would have produced 3 "overlapping" points
    # (min(len(AAPL)=4, len(MSFT)=3, len(BENCH)=3) == 3), silently pairing
    # returns from mismatched calendar dates. Correct date-based alignment
    # keeps only the two dates (1/4, 1/5) where AAPL, MSFT, and BENCH all
    # have data.
    assert len(captured["portfolio_returns"]) == 2
    assert len(captured["benchmark_returns"]) == 2

    portfolio_1_4 = (103.0 / 102.0 - 1 + 204.0 / 202.0 - 1) / 2
    portfolio_1_5 = (104.0 / 103.0 - 1 + 206.0 / 204.0 - 1) / 2
    bench_1_4 = 52.0 / 51.0 - 1
    bench_1_5 = 53.0 / 52.0 - 1

    assert captured["portfolio_returns"] == pytest.approx([portfolio_1_4, portfolio_1_5])
    assert captured["benchmark_returns"] == pytest.approx([bench_1_4, bench_1_5])
    # The dates travel with the returns so the factor regression can join the
    # (lagged) Fama-French series on date instead of on list position.
    assert captured["dates"] == ["2024-01-04", "2024-01-05"]


def test_factor_attribution_restates_each_series_from_its_listing_currency(monkeypatch):
    """Ken French's factors are USD returns: a EUR listing is converted, a USD
    one is not (ADR 0007 amendment 2). The currency is the listing's
    resolved one, not the bars' stored label: the old ingester labelled
    AAPL's bars "EUR", and the LSE quotes IWDA.L in USD, not pounds."""
    import pandas as pd

    from app.foundation.data_backbone.listing_currency import record_listing_currency
    from app.interface.api import attribution as attribution_api

    days = ["2024-01-01", "2024-01-02", "2024-01-03"]
    rates = {"2024-01-01": 1.10, "2024-01-02": 1.111, "2024-01-03": 1.111}
    asked: list[str] = []

    def usd_per_unit(db, ccy, days=0, allow_live=True):
        asked.append(ccy)
        return rates if ccy == "EUR" else None

    monkeypatch.setattr(attribution_api, "usd_per_unit_by_date", usd_per_unit)
    frame = pd.DataFrame({"ts": pd.to_datetime(days), "close": [100.0, 101.0, 101.0]})
    mislabelled = frame.assign(currency="EUR")
    db = _memory_db()
    record_listing_currency(db, "IWDA.L", "USD", "yfinance_metadata")
    db.commit()

    eur = attribution_api._usd_indexed_returns(db, "SAP.DE", frame)
    aapl = attribution_api._usd_indexed_returns(db, "AAPL", mislabelled)
    iwda = attribution_api._usd_indexed_returns(db, "IWDA.L", mislabelled)

    assert eur.iloc[0] == pytest.approx(1.01 * 1.01 - 1)
    assert eur.iloc[1] == pytest.approx(0.0)
    assert aapl.tolist() == pytest.approx([0.01, 0.0])
    assert iwda.tolist() == pytest.approx([0.01, 0.0])
    assert asked == ["EUR"]

