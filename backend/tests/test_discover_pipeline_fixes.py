"""Regression tests for the Discover pipeline fixes (June 2026).

Covers:
- region-aware benchmark selection;
- portfolio_fit aligning a date-indexed candidate with the datetime64-indexed
  portfolio matrix (the bug that rejected all 55 candidates on the deployment);
- quality breaches becoming advisory concerns instead of hard rejects;
- backtest failing open when the benchmark cannot be fetched.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pandas as pd
from conftest import _memory_db

from app.foundation.models.entities import DkbAccount, DkbPosition, PriceCache, User
from app.decision.discover.pipeline import (
    _pick_benchmark,
    stage_backtest_vs_benchmark,
    stage_momentum_quality,
    stage_portfolio_fit,
)


def _seed_prices(db, ticker: str, closes: list[float], end: date | None = None) -> None:
    """Seed PriceCache with business-day closes ending at *end* (today by default)."""
    end = end or date.today()
    # Walk back business days from `end` for len(closes) points.
    days: list[date] = []
    d = end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    now = datetime.now(UTC)
    # The synthetic tickers have no listing suffix; mark them EUR listings so
    # the EUR restatement keeps them (FX is tested in test_eur_prices).
    from app.foundation.models.entities import ListingCurrency
    if db.get(ListingCurrency, ticker.upper()) is None:
        db.add(ListingCurrency(symbol=ticker.upper(), currency="EUR", source="test"))
    for dd, close in zip(days, closes):
        db.add(PriceCache(
            id=uuid4().hex, ticker=ticker.upper(), date=dd,
            close=Decimal(str(round(close, 4))), fetched_at=now,
            source="test", stale=False, currency="EUR",
        ))
    db.commit()


def test_pick_benchmark_is_region_aware():
    assert _pick_benchmark("SAP.DE", is_etf=False) == "VGK"
    assert _pick_benchmark("MC.PA", is_etf=False) == "VGK"
    assert _pick_benchmark("ASML.AS", is_etf=False) == "VGK"
    assert _pick_benchmark("AAPL", is_etf=False) == "SPY"
    # Tokyo used to fall through to SPY.
    assert _pick_benchmark("7203.T", is_etf=False) == "EWJ"
    assert _pick_benchmark("0700.HK", is_etf=False) == "URTH"
    assert _pick_benchmark("INTC", is_etf=False) == "SPY"
    assert _pick_benchmark("VWCE.DE", is_etf=True) == "URTH"


def test_portfolio_fit_aligns_date_indexed_candidate_with_portfolio():
    """The core bug: a date-indexed candidate must align with the datetime64 matrix.

    Both series cover the same ~250 business days. The fix normalises the candidate
    index to datetime64 so the intersection is the full overlap (not empty).
    """
    db = _memory_db()
    user = User(id=uuid4().hex, username="u", password_hash="x")
    db.add(user)
    db.commit()

    n = 260
    cand_closes = [100.0 + i * 0.1 for i in range(n)]
    port_closes = [50.0 + (i % 7) * 0.2 for i in range(n)]  # weakly correlated
    _seed_prices(db, "CAND", cand_closes)
    _seed_prices(db, "PORT", port_closes)

    account = DkbAccount(id=uuid4().hex, user_id=user.id, type="depot",
                         iban="DE" + uuid4().hex[:20], balance=Decimal("0"), currency="EUR")
    db.add(account)
    db.commit()
    db.add(DkbPosition(id=uuid4().hex, account_id=account.id, isin="X",
                       ticker="PORT", name="Port", quantity=Decimal("1"),
                       avg_buy_price=Decimal("50"), current_price=Decimal("60"),
                       current_value=Decimal("60")))
    db.commit()

    scores, reject = stage_portfolio_fit(db, user.id, "CAND")

    assert reject is None
    assert scores is not None
    # A real correlation was computed (not the fail-open neutral note path).
    assert scores["correlation"] is not None
    assert isinstance(scores["fit_score"], float)
    assert "note" not in scores


def test_portfolio_fit_failopen_when_no_overlap():
    """Genuinely non-overlapping candidate → neutral fit, NOT a rejection."""
    db = _memory_db()
    user = User(id=uuid4().hex, username="u", password_hash="x")
    db.add(user)
    db.commit()

    # Candidate prices a year ago; portfolio prices recent → little/no overlap.
    old_end = date.today() - timedelta(days=420)
    _seed_prices(db, "CAND", [100.0 + i * 0.1 for i in range(120)], end=old_end)
    _seed_prices(db, "PORT", [50.0 + i * 0.1 for i in range(200)])

    account = DkbAccount(id=uuid4().hex, user_id=user.id, type="depot",
                         iban="DE" + uuid4().hex[:20], balance=Decimal("0"), currency="EUR")
    db.add(account)
    db.commit()
    db.add(DkbPosition(id=uuid4().hex, account_id=account.id, isin="X",
                       ticker="PORT", name="Port", quantity=Decimal("1"),
                       avg_buy_price=Decimal("50"), current_price=Decimal("60"),
                       current_value=Decimal("60")))
    db.commit()

    scores, reject = stage_portfolio_fit(db, user.id, "CAND")
    # Candidate has < 60 rows in the last 365d window → candidate-data guard, OR
    # neutral fail-open. Either way it must not be the silent alignment rejection.
    assert reject is None or "insufficient aligned observations" not in (reject or "")
    if reject is None:
        assert scores is not None
        assert scores["fit_score"] == 0.5


def test_momentum_quality_breach_is_advisory_not_reject():
    """A -40% momentum stock must reach the LLM with a concern flag, not be rejected."""
    db = _memory_db()
    # Steady decline over ~300 business days → strongly negative 12m momentum.
    closes = [200.0 * (0.995 ** i) for i in range(300)]
    _seed_prices(db, "DOWN", closes)

    scores, reject = stage_momentum_quality(db, "DOWN")

    assert reject is None
    assert scores is not None
    assert scores["momentum_12_1m"] < -0.20
    assert any("momentum" in c for c in scores["concerns"])


def test_momentum_quality_trailing_1m_annualized_return_tracks_recent_trend():
    """trailing_1m_annualized_return reflects the last ~21 trading days, not the full window.

    A money-market-style series flat for a year then stepping up in the final
    month should show a near-zero momentum_12_1m (unrepresentative of the
    money-market case, but not what's under test here) while the trailing 1m
    figure picks up the recent step — it's the anchor money-market candidates
    use precisely because it reacts fast to a rate change (see dossier_writer.py).
    """
    db = _memory_db()
    flat = [100.0] * 279
    stepped_up = [100.0 * (1.0002 ** i) for i in range(1, 22)]  # ~5% annualized daily drift
    closes = flat + stepped_up
    _seed_prices(db, "MMKT", closes)

    scores, reject = stage_momentum_quality(db, "MMKT")

    assert reject is None
    assert scores is not None
    assert "trailing_1m_annualized_return" in scores
    assert scores["trailing_1m_annualized_return"] > 0.03


def test_backtest_failopen_when_benchmark_missing():
    """No benchmark data → neutral signal (excess=None), never a rejection."""
    db = _memory_db()
    _seed_prices(db, "CAND", [100.0 + i * 0.05 for i in range(300)])
    # Deliberately seed NO data for the benchmark (VGK).

    scores, reject = stage_backtest_vs_benchmark(db, "CAND.DE", is_etf=False)

    assert reject is None
    assert scores is not None
    assert scores["benchmark"] == "VGK"
    assert scores["excess_return_annual"] is None


def test_backtest_vs_benchmark_applies_fx_adjustment():
    """A EUR candidate's excess return vs the USD-listed VGK is FX-adjusted.

    Seed VGK with a *flat* USD price and a USD->EUR rate that appreciates
    steadily. Unconverted, VGK's return is ~0%, so any EURUSD drift would show
    up entirely as CAND's "excess" return relative to a genuinely flat
    benchmark. With the FX adjustment, VGK's EUR-terms return absorbs that
    drift instead, so CAND's excess return should reflect (close to) CAND's
    own price appreciation, not CAND's appreciation plus the FX drift.
    """
    from app.decision.discover import pipeline as pipeline_module

    db = _memory_db()
    n = 300
    cand_closes = [100.0 * (1.0002 ** i) for i in range(n)]  # ~6.5%/yr
    vgk_closes = [80.0 for _ in range(n)]  # flat in USD
    fx_closes = [0.90 * (1.0005 ** i) for i in range(n)]  # USD strengthens vs EUR

    _seed_prices(db, "CAND.DE", cand_closes)
    _seed_prices(db, "VGK", vgk_closes)
    _seed_prices(db, "USDEUR=X", fx_closes)

    pipeline_module._FX_SERIES_CACHE.clear()
    scores, reject = stage_backtest_vs_benchmark(db, "CAND.DE", is_etf=False)
    pipeline_module._FX_SERIES_CACHE.clear()

    assert reject is None
    assert scores is not None
    # Unconverted, VGK's flat USD price shows ~0% return, so CAND's excess
    # would equal CAND's own ~6.5%/yr appreciation. FX-adjusted, VGK gains
    # the USDEUR drift (~13%/yr) in EUR terms, so excess return should be
    # meaningfully lower than CAND's own unadjusted return.
    assert scores["excess_return_annual"] < 0.03


def test_candidate_index_is_date_typed_from_market_history():
    """Document the precondition: market_history yields datetime.date dates."""
    db = _memory_db()
    _seed_prices(db, "CAND", [100.0, 101.0, 102.0])
    from app.foundation.market import history as market_history

    rows = market_history(db, "CAND", days=365, allow_live=False)
    assert rows
    assert isinstance(rows[0]["date"], date)
    # And pandas normalisation makes it datetime64 (the fix).
    idx = pd.to_datetime(pd.Index([r["date"] for r in rows]))
    assert str(idx.dtype) == "datetime64[ns]"


def test_portfolio_fit_sees_through_asynchronous_closes():
    """A candidate that follows the portfolio's move one trading day late --
    a US close against Xetra-listed holdings -- is not a diversifier. Daily
    returns put its correlation near zero; weekly returns recover it
    (Discover run audit, 2026-09-28)."""
    import numpy as np

    db = _memory_db()
    user = User(id=uuid4().hex, username="u", password_hash="x")
    db.add(user)
    db.commit()

    rng = np.random.default_rng(7)
    common = rng.normal(0.0, 0.01, 521)
    port_closes = list(50.0 * np.cumprod(1.0 + common[1:]))
    cand_closes = list(100.0 * np.cumprod(1.0 + common[:-1]))  # one day late
    _seed_prices(db, "CAND", cand_closes)
    _seed_prices(db, "PORT", port_closes)
    daily = np.corrcoef(np.diff(cand_closes) / cand_closes[:-1], np.diff(port_closes) / port_closes[:-1])[0, 1]
    assert abs(daily) < 0.2

    account = DkbAccount(id=uuid4().hex, user_id=user.id, type="depot",
                         iban="DE" + uuid4().hex[:20], balance=Decimal("0"), currency="EUR")
    db.add(account)
    db.commit()
    db.add(DkbPosition(id=uuid4().hex, account_id=account.id, isin="X",
                       ticker="PORT", name="Port", quantity=Decimal("1"),
                       avg_buy_price=Decimal("50"), current_price=Decimal("60"),
                       current_value=Decimal("60")))
    db.commit()

    scores, reject = stage_portfolio_fit(db, user.id, "CAND")

    assert reject is None
    assert scores["correlation_basis"] == "weekly"
    assert scores["correlation_weeks"] >= 100
    assert scores["correlation"] > 0.6
