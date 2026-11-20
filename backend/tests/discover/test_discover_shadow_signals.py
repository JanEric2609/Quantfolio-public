"""Signals recorded for every candidate but not ranked on (plan 2026-11-08, WP3)."""
import numpy as np
import pandas as pd

from app.decision.discover import pipeline
from app.decision.discover.shadow_ledger import SHADOW_SIGNAL_KEYS, shadow_signals


def _legs(residual_drift: float, months: int = 40, seed: int = 3):
    """Daily closes of a benchmark and a stock = 1.2 x benchmark + noise + a drift in the last year."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp("2026-11-06"), periods=months * 21)
    bench_r = rng.normal(0.0004, 0.01, len(dates))
    noise = rng.normal(0, 0.006, len(dates))
    drift = np.where(np.arange(len(dates)) >= len(dates) - 12 * 21, residual_drift, 0.0)
    cand_r = 1.2 * bench_r + noise + drift
    return dates, 100 * np.cumprod(1 + cand_r), 100 * np.cumprod(1 + bench_r)


def test_residual_momentum_sees_what_the_market_does_not_explain():
    up = pipeline._residual_momentum(*_legs(0.002))
    down = pipeline._residual_momentum(*_legs(-0.002))
    assert up is not None and down is not None
    assert up > 2.0 > -2.0 > down


def test_residual_momentum_needs_two_years_of_months():
    assert pipeline._residual_momentum(*_legs(0.002, months=20)) is None


def test_welch_beta_is_not_pulled_by_one_jump():
    rng = np.random.default_rng(7)
    market = rng.normal(0, 0.01, 250)
    stock = 0.9 * market + rng.normal(0, 0.004, 250)
    stock[100] = 0.60  # one +60 % day on a normal market day
    market[100] = 0.01
    ols = float(np.cov(stock, market)[0, 1] / np.var(market, ddof=1))
    welch = pipeline._welch_beta(stock.tolist(), market.tolist())
    assert welch is not None
    assert abs(welch - 0.9) < 0.1
    assert abs(ols - 0.9) > abs(welch - 0.9)


def test_welch_beta_needs_enough_days():
    assert pipeline._welch_beta([0.01] * 30, [0.01] * 30) is None


def test_marginal_vol_says_whether_a_position_lowers_the_book_risk():
    rng = np.random.default_rng(11)
    book = pd.Series(rng.normal(0.002, 0.02, 104))
    # Low correlation but four times the volatility still raises the book's risk.
    wild = pd.Series(0.3 * book * 4 + rng.normal(0, 0.077, 104))
    corr_wild = float(wild.corr(book))
    raises = pipeline._marginal_risk(wild, book, corr_wild)
    hedge = pd.Series(-0.5 * book + rng.normal(0, 0.01, 104))
    lowers = pipeline._marginal_risk(hedge, book, float(hedge.corr(book)))
    assert corr_wild < 0.5
    assert raises["marginal_vol"] is not None and raises["marginal_vol"] > 0
    assert lowers["marginal_vol"] is not None and lowers["marginal_vol"] < 0
    assert lowers["downside_correlation"] is not None and lowers["downside_correlation"] < 0


def test_downside_correlation_needs_enough_falling_weeks():
    book = pd.Series([0.01] * 50 + [-0.01] * 5)
    assert pipeline._marginal_risk(book * 2, book, 1.0)["downside_correlation"] is None


def test_snapshot_takes_the_shadow_signals_present():
    scores = {
        "backtest_vs_benchmark": {"residual_momentum": 1.7},
        "momentum_quality": {"high_52w_ratio": 0.93, "information_discreteness": -0.12},
        "portfolio_fit": {"marginal_vol": -0.01, "downside_correlation": None},
    }
    assert shadow_signals(scores) == {
        "residual_momentum": 1.7, "high_52w_ratio": 0.93, "information_discreteness": -0.12, "marginal_vol": -0.01,
    }
    assert set(SHADOW_SIGNAL_KEYS) >= set(shadow_signals(scores))


def test_the_composite_does_not_read_the_shadow_signals():
    from app.decision.discover.composite import derive_signals_from_scores

    base = {
        "momentum_quality": {"momentum_12_1m": 0.2, "volatility_6m": 0.2, "max_drawdown": -0.1, "momentum_rank": 0.7},
        "backtest_vs_benchmark": {"information_ratio": 0.3, "active_t_stat": 0.5},
        "portfolio_fit": {"fit_score": 0.6, "correlation": 0.4},
    }
    with_shadow = {
        "momentum_quality": {**base["momentum_quality"], "high_52w_ratio": 0.5, "information_discreteness": 0.4},
        "backtest_vs_benchmark": {**base["backtest_vs_benchmark"], "residual_momentum": -3.0},
        "portfolio_fit": {**base["portfolio_fit"], "marginal_vol": 0.2, "downside_correlation": 0.9},
    }
    assert derive_signals_from_scores(base) == derive_signals_from_scores(with_shadow)


def test_shadow_signal_ics_are_measured_on_the_whole_cross_section():
    from datetime import UTC, date, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.decision.verification.ranking_metrics import shadow_signal_ics
    from app.foundation.core.db import Base
    from app.foundation.models.entities import CandidateOutcome, DiscoverCandidateSnapshot, User

    def _memory_db():
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    db = _memory_db()
    user = User(username="shadow", password_hash="x")
    db.add(user)
    db.commit()
    rng = np.random.default_rng(5)
    for k in range(8):
        day = date(2026, 6, 1) + timedelta(weeks=k)
        for i in range(40):
            good = float(i) / 40
            snap = DiscoverCandidateSnapshot(
                user_id=user.id, run_id=f"r{k}", issued_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                issue_date=day, symbol=f"S{i}", instrument_group="stock", sector="Tech", evaluable=True,
                composite=0.5, components_json={"shadow_signals": {"residual_momentum": good, "marginal_vol": 0.1}},
                n_evaluable_stocks=40, shortlisted=False, sector_capped=False, picked=False, cohort_id="c",
                provenance_json={}, backfilled=False,
            )
            db.add(snap)
            db.flush()
            ret = 0.05 * good + float(rng.normal(0, 0.005))
            db.add(CandidateOutcome(
                snapshot_id=snap.id, user_id=user.id, run_id=snap.run_id, symbol=snap.symbol, horizon_days=21,
                target_date=day + timedelta(days=30), ret_eur=ret, bench_ret_eur=0.0, excess_eur=ret,
                status="resolved", benchmark="EUNL.DE",
            ))
    db.commit()

    ics = shadow_signal_ics(db, user.id)
    assert ics["residual_momentum"]["n_runs"] == 8
    assert ics["residual_momentum"]["mean"] > 0.8
    # A constant signal has no rank, so no IC.
    assert ics["marginal_vol"]["n_runs"] == 0
