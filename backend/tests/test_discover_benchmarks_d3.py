"""Benchmarks by asset class, both legs in EUR (plan 2026-10-12, D3 / WP1c)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import _memory_db

from app.decision.discover.pipeline import stage_backtest_vs_benchmark
from app.foundation.estr import closes_on, index_levels, store_fixings
from app.foundation.models.entities import ListingCurrency, PriceCache
from app.foundation.quant_metrics import annualised_return


def _business_days(n: int) -> list[date]:
    days: list[date] = []
    d = date.today()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _seed(db, ticker: str, currency: str, closes: list[float], days: list[date]) -> None:
    if db.get(ListingCurrency, ticker) is None:
        db.add(ListingCurrency(symbol=ticker, currency=currency, source="test"))
    now = datetime.now(UTC)
    for d, c in zip(days, closes):
        db.add(PriceCache(id=uuid4().hex, ticker=ticker, date=d, close=Decimal(str(round(c, 6))),
                          fetched_at=now, source="test", stale=False, currency=currency))
    db.commit()


def test_usd_stock_against_accumulating_eur_benchmark_matches_hand_calculation():
    db = _memory_db()
    n = 300
    days = _business_days(n)
    stock_usd = [50.0 * (1.0004 ** i) for i in range(n)]
    eunl_eur = [90.0 * (1.0002 ** i) for i in range(n)]
    usdeur = [0.92 * (1.0001 ** i) for i in range(n)]  # EUR per USD, rising
    _seed(db, "ACME", "USD", stock_usd, days)
    _seed(db, "EUNL.DE", "EUR", eunl_eur, days)
    _seed(db, "USDEUR=X", "EUR", usdeur, days)

    scores, reject = stage_backtest_vs_benchmark(db, "ACME", asset_class="equity_broad")

    assert reject is None and scores is not None
    assert scores["benchmark"] == "EUNL.DE"
    stock_eur = [s * fx for s, fx in zip(stock_usd, usdeur)]
    cand = [stock_eur[i] / stock_eur[i - 1] - 1 for i in range(1, n)]
    bench = [eunl_eur[i] / eunl_eur[i - 1] - 1 for i in range(1, n)]
    expected = annualised_return(cand) - annualised_return(bench)
    assert scores["excess_return_annual"] == pytest.approx(expected, abs=1e-4)


def test_gold_has_no_excess_return_and_no_concern():
    db = _memory_db()
    scores, reject = stage_backtest_vs_benchmark(db, "4GLD.DE", asset_class="gold")
    assert reject is None and scores is not None
    assert scores["benchmark"] is None
    assert scores["excess_return_annual"] is None
    assert scores["concerns"] == []


def test_estr_index_compounds_act_360_over_weekends():
    fixings = [(date(2026, 10, 8), 2.4), (date(2026, 10, 9), 2.4), (date(2026, 10, 12), 2.4)]
    levels = index_levels(fixings)
    assert levels["2026-10-09"] == pytest.approx(100 * (1 + 0.024 / 360))
    # Friday's fixing earns three days.
    assert levels["2026-10-12"] == pytest.approx(levels["2026-10-09"] * (1 + 0.024 * 3 / 360))


def test_money_market_fund_is_scored_against_compounded_estr():
    db = _memory_db()
    n = 300
    days = _business_days(n)
    store_fixings(db, [{"date": d.isoformat(), "value": 3.0} for d in days])
    db.commit()
    estr = closes_on(db, [d.isoformat() for d in days])
    # A fund that tracks €STR minus a 0.10 % fee.
    fund = [estr[d.isoformat()] * (1 - 0.001 / 252) ** i for i, d in enumerate(days)]
    _seed(db, "XEON.DE", "EUR", fund, days)

    scores, reject = stage_backtest_vs_benchmark(db, "XEON.DE", asset_class="money_market")

    assert reject is None and scores is not None
    assert scores["benchmark"] == "ESTR"
    assert scores["excess_return_annual"] == pytest.approx(-0.001, abs=3e-4)
