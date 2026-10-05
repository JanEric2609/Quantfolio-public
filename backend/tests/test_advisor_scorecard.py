"""Tests for the 4-axis advisor scorecard (advisor loop E1/E2).

Acceptance: seed predictions with known realised prices → the four axes
compute to expected values (Brier for a known confidence/outcome pair, MZ
slope for a linear fixture, Sharpe for a known return series).
"""
import math
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorScorecard,
    AdvisorStrategy,
    DiscoveryPrediction,
    MetricsSnapshot,
    PaperPortfolio,
    PaperSnapshot,
    User,
)
from app.foundation.models.entities._core import now_utc
from app.decision.advisor.cycle import ADVISOR_MANDATE
from app.decision.advisor.scorecard import (
    compute_advisor_scorecard,
    compute_brier_and_log_loss,
    compute_bucketed_rps,
)
from app.foundation.quant_metrics import (
    CALENDAR_DAYS_PER_YEAR,
    compute_mincer_zarnowitz,
    sharpe_ratio,
    sharpe_standard_error,
)


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _advisor_portfolio(db, user) -> PaperPortfolio:
    p = PaperPortfolio(
        user_id=user.id, name="Advisor Loop", mandate=ADVISOR_MANDATE,
        managed_by="llm", initial_cash=Decimal("10000"),
    )
    db.add(p)
    db.commit()
    return p


def _resolved_prediction(
    db, user, *, conviction, expected, realised, low=None, high=None
) -> DiscoveryPrediction:
    pred = DiscoveryPrediction(
        id=uuid4().hex, user_id=user.id, run_id="r1", symbol="TST",
        predicted_at=now_utc() - timedelta(days=30),
        horizon_days=21, resolve_at=now_utc() - timedelta(days=1),
        direction="buy", conviction=conviction, expected_return=expected,
        expected_return_low=low, expected_return_high=high,
        realised_return=realised, outcome_status="resolved",
        price_at_prediction=100.0, features_json={},
    )
    db.add(pred)
    db.commit()
    return pred


# ---------------------------------------------------------------------------
# Axis primitives
# ---------------------------------------------------------------------------


def test_brier_known_pair():
    """Confidence 0.8 on a hit → Brier (0.8-1)^2 = 0.04; log-loss -ln(0.8)."""
    brier, log_loss = compute_brier_and_log_loss([(0.8, 1)])
    assert brier == pytest.approx(0.04)
    assert log_loss == pytest.approx(-math.log(0.8))


def test_brier_averages_pairs():
    brier, _ = compute_brier_and_log_loss([(0.8, 1), (0.6, 0)])
    assert brier == pytest.approx(((0.8 - 1) ** 2 + (0.6 - 0) ** 2) / 2)


def test_mincer_zarnowitz_linear_fixture():
    """realised = exactly 1.0 × predicted → slope 1, R² 1."""
    pairs = [(0.01, 0.01), (0.05, 0.05), (-0.02, -0.02), (0.10, 0.10)]
    slope, r2 = compute_mincer_zarnowitz(pairs)
    assert slope == pytest.approx(1.0)
    assert r2 == pytest.approx(1.0)


def test_mincer_zarnowitz_half_slope():
    """realised = 0.5 × predicted → slope 0.5."""
    pairs = [(p, 0.5 * p) for p in (0.02, 0.04, -0.03, 0.08, -0.01)]
    slope, r2 = compute_mincer_zarnowitz(pairs)
    assert slope == pytest.approx(0.5)
    assert r2 == pytest.approx(1.0)


def test_mincer_zarnowitz_insufficient_data():
    assert compute_mincer_zarnowitz([(0.01, 0.02)]) == (None, None)


def test_bucketed_rps_well_calibrated_scores_better_than_miscalibrated():
    """RPS is a proper scoring rule — it isn't exactly 0 even for a
    well-calibrated sample (each individual outcome is binary, not
    distributed), but a sample matching the predicted 90/5/5 split must
    score strictly better than one where the band never captures anything."""
    well_calibrated = (
        [(-0.05, 0.05, 0.0)] * 90  # inside band
        + [(-0.05, 0.05, -0.10)] * 5  # below p5
        + [(-0.05, 0.05, 0.10)] * 5  # above p95
    )
    always_outside = [(-0.05, 0.05, -0.10)] * 100  # band never captures anything

    rps_calibrated = compute_bucketed_rps(well_calibrated)
    rps_miscalibrated = compute_bucketed_rps(always_outside)
    assert rps_calibrated < rps_miscalibrated


def test_bucketed_rps_penalizes_band_that_never_captures_outcome():
    """A band that NEVER captures the realised outcome (always outside) is
    badly miscalibrated and must score much worse than perfect calibration."""
    always_below = [(-0.05, 0.05, -0.10)] * 20  # always below p5
    rps = compute_bucketed_rps(always_below)
    perfectly_calibrated = compute_bucketed_rps(
        [(-0.05, 0.05, 0.0)] * 18 + [(-0.05, 0.05, -0.10)] + [(-0.05, 0.05, 0.10)]
    )
    assert rps > perfectly_calibrated


def test_bucketed_rps_empty_returns_none():
    assert compute_bucketed_rps([]) is None


def test_bucketed_rps_handles_swapped_low_high():
    """Defensive: low > high (shouldn't happen from real MC output, but the
    function must not silently misclassify if it does)."""
    rps_normal = compute_bucketed_rps([(-0.05, 0.05, 0.0)])
    rps_swapped = compute_bucketed_rps([(0.05, -0.05, 0.0)])
    assert rps_normal == pytest.approx(rps_swapped)


# ---------------------------------------------------------------------------
# Full scorecard
# ---------------------------------------------------------------------------


def _seed_snapshots(db, portfolio, daily_returns: list[float], start_value=10_000.0):
    value = start_value
    day = date.today() - timedelta(days=len(daily_returns) + 1)
    db.add(PaperSnapshot(
        portfolio_id=portfolio.id, date=day, total_value=Decimal(str(value)),
        cash_balance=Decimal("0"), securities_value=Decimal(str(value)),
        total_return_pct=Decimal("0"), currency="EUR",
    ))
    for r in daily_returns:
        day += timedelta(days=1)
        value *= 1 + r
        db.add(PaperSnapshot(
            portfolio_id=portfolio.id, date=day, total_value=Decimal(str(round(value, 4))),
            cash_balance=Decimal("0"), securities_value=Decimal(str(round(value, 4))),
            total_return_pct=Decimal("0"), currency="EUR",
        ))
    db.commit()


def test_scorecard_computes_all_four_axes():
    db = _memory_db()
    user = _user(db)
    portfolio = _advisor_portfolio(db, user)

    # Calibration + magnitude cohort
    _resolved_prediction(db, user, conviction=0.8, expected=0.05, realised=0.05)
    _resolved_prediction(db, user, conviction=0.6, expected=0.02, realised=0.02)
    _resolved_prediction(db, user, conviction=0.7, expected=-0.03, realised=-0.03)

    # Known NAV series. 70 returns clears both sample-size floors
    # (MIN_RETURNS_FOR_RATIOS=30, MIN_RETURNS_FOR_CVAR=60) and still fits
    # inside the 90-day window. Deterministic, so the golden Sharpe below is
    # reproducible.
    base = [0.01, -0.005, 0.008, 0.002, -0.001, 0.004, 0.006]
    returns = [round(base[i % len(base)] * (1 + 0.01 * (i % 5)), 6) for i in range(70)]
    _seed_snapshots(db, portfolio, returns)

    row = compute_advisor_scorecard(db, user.id)
    assert row is not None

    # Axis 1: sharpe matches quant_metrics on the same series (approx — NAV
    # decimals round-trip through Decimal). PaperSnapshot rows are daily
    # including weekends, so the series is calendar-cadence and must be
    # annualised the same way paper_portfolio.compute_metrics does — asserting
    # the 252-day default here pinned a bug in which the same paper portfolio
    # reported two Sharpes ~20% apart into the same metrics_snapshots table.
    assert row.sharpe == pytest.approx(
        sharpe_ratio(returns, periods_per_year=CALENDAR_DAYS_PER_YEAR), abs=0.05
    )
    assert row.sortino is not None and row.calmar is not None

    # PSR (F11): a probability in [0,1], computed with the resolved
    # risk-free rate (never 0.0 silently).
    from app.foundation.quant_metrics import probabilistic_sharpe_ratio
    from app.foundation.settings import get_risk_free_rate

    assert row.psr is not None
    assert 0.0 <= row.psr <= 1.0
    assert row.psr == pytest.approx(
        probabilistic_sharpe_ratio(
            returns, risk_free=get_risk_free_rate(db), periods_per_year=CALENDAR_DAYS_PER_YEAR
        ),
        abs=1e-6,
    )
    # DSR (ADR 0015, Finding F15): n_trials is now the global, ledger-backed
    # count (resolve_n_trials), not this user's AdvisorStrategy row count —
    # an empty ledger floors at 500, so DSR is always computed once returns
    # clear the sample-size floor, rather than staying None whenever no
    # AdvisorStrategy rows happen to exist.
    assert row.dsr is not None
    assert 0.0 <= row.dsr <= 1.0

    # Axis 2: Brier for known pairs — hits are (0.8,1),(0.6,1),(0.7,0)
    expected_brier = ((0.8 - 1) ** 2 + (0.6 - 1) ** 2 + (0.7 - 0) ** 2) / 3
    assert row.brier_avg == pytest.approx(expected_brier)
    assert row.log_loss_avg is not None

    # Axis 3: realised == predicted → slope 1, R² 1
    assert row.mz_slope == pytest.approx(1.0)
    assert row.mz_r2 == pytest.approx(1.0)

    # Axis 4: downside metrics present and sane
    assert row.max_drawdown is not None and row.max_drawdown >= 0
    assert row.cvar_95 is not None

    # Every reported Sharpe carries its Lo (2002) standard error, so a caller
    # can tell a ranked figure from an unrankable one.
    assert row.details_json["sharpe_se"] == pytest.approx(
        sharpe_standard_error(row.sharpe, 70, periods_per_year=CALENDAR_DAYS_PER_YEAR),
        rel=1e-6,
    )
    assert row.details_json["sharpe_ci_spans_zero"] is False

    assert row.n_resolved == 3


def test_ratios_are_null_below_the_sample_floor():
    """An annualised Sharpe from a handful of daily returns is not a measurement.

    At the previous n>=5 guard the 95% interval on an annualised Sharpe of 1.0
    was about +/-16.8 (Lo 2002), and the 95% CVaR tail held a single
    observation — making "expected shortfall" definitionally equal to VaR.
    Both now return NULL rather than a number the UI would rank on.
    """
    db = _memory_db()
    user = _user(db)
    portfolio = _advisor_portfolio(db, user)
    _seed_snapshots(db, portfolio, [0.01, -0.005, 0.008, 0.002, -0.001, 0.004, 0.006])

    row = compute_advisor_scorecard(db, user.id)
    assert row is not None
    assert row.sharpe is None and row.sortino is None and row.calmar is None
    assert row.cvar_95 is None
    assert row.details_json["n_nav_returns"] == 7
    assert row.details_json["sharpe_se"] is None


def test_cvar_has_a_higher_floor_than_the_ratios():
    """A tail estimator needs more data than a moment estimator.

    At 40 returns the ratios are reportable but the 95% tail still holds only
    three observations, so CVaR stays NULL while Sharpe/Sortino/Calmar populate.
    """
    db = _memory_db()
    user = _user(db)
    portfolio = _advisor_portfolio(db, user)
    base = [0.01, -0.005, 0.008, 0.002, -0.001, 0.004, 0.006]
    _seed_snapshots(db, portfolio, [base[i % len(base)] for i in range(40)])

    row = compute_advisor_scorecard(db, user.id)
    assert row is not None
    assert row.sharpe is not None and row.sortino is not None and row.calmar is not None
    assert row.cvar_95 is None
    assert row.details_json["sharpe_se"] is not None


def test_scorecard_computes_rps_avg_from_mc_band_and_realised_return():
    """F11: rps_avg is computed from expected_return_low/high + realised_return,
    independent of the sign-only brier_avg (which also gets computed)."""
    db = _memory_db()
    user = _user(db)
    _advisor_portfolio(db, user)

    # Well-calibrated: realised lands inside the predicted band.
    _resolved_prediction(db, user, conviction=0.8, expected=0.02, realised=0.02, low=-0.05, high=0.05)
    _resolved_prediction(db, user, conviction=0.6, expected=0.01, realised=0.01, low=-0.04, high=0.06)
    _resolved_prediction(db, user, conviction=0.7, expected=-0.01, realised=-0.01, low=-0.06, high=0.04)

    row = compute_advisor_scorecard(db, user.id)
    assert row is not None
    assert row.rps_avg is not None
    assert 0.0 <= row.rps_avg <= 1.0
    assert row.brier_avg is not None  # still computed independently
    assert row.details_json["n_rps_triples"] == 3


def test_scorecard_pending_axes_stay_null():
    """No matured predictions + too few snapshots → axes NULL, never fabricated."""
    db = _memory_db()
    user = _user(db)
    _advisor_portfolio(db, user)

    row = compute_advisor_scorecard(db, user.id)
    assert row is not None
    assert row.sharpe is None
    assert row.brier_avg is None
    assert row.mz_slope is None
    assert row.max_drawdown is None
    assert row.n_resolved == 0


def test_scorecard_upserts_per_window_end():
    db = _memory_db()
    user = _user(db)
    _advisor_portfolio(db, user)

    first = compute_advisor_scorecard(db, user.id)
    second = compute_advisor_scorecard(db, user.id)
    assert first.id == second.id
    assert db.query(AdvisorScorecard).count() == 1


def test_scorecard_none_without_advisor_portfolio():
    db = _memory_db()
    user = _user(db)
    assert compute_advisor_scorecard(db, user.id) is None


def test_scorecard_writes_metrics_snapshot():
    """Phase 4 (unified-portfolio-engine-implementation.md): axes 1+4 are
    also mirrored into the shared metrics_snapshots table, keyed by
    context="advisor_scorecard", so cross-portfolio consumers don't need to
    know about AdvisorScorecard's bespoke columns."""
    db = _memory_db()
    user = _user(db)
    portfolio = _advisor_portfolio(db, user)
    returns = [0.01, -0.005, 0.008, 0.002, -0.001, 0.004, 0.006]
    _seed_snapshots(db, portfolio, returns)

    row = compute_advisor_scorecard(db, user.id)

    snap = (
        db.query(MetricsSnapshot)
        .filter(MetricsSnapshot.portfolio_id == portfolio.id, MetricsSnapshot.context == "advisor_scorecard")
        .one()
    )
    assert snap.as_of == row.window_end
    assert snap.sharpe == pytest.approx(row.sharpe)
    assert snap.sortino == pytest.approx(row.sortino)
    assert snap.calmar == pytest.approx(row.calmar)
    assert snap.max_drawdown == pytest.approx(row.max_drawdown)
    assert snap.cvar_95 == pytest.approx(row.cvar_95)


def test_scorecard_metrics_snapshot_upserts_per_window_end():
    db = _memory_db()
    user = _user(db)
    _advisor_portfolio(db, user)

    compute_advisor_scorecard(db, user.id)
    compute_advisor_scorecard(db, user.id)

    assert db.query(MetricsSnapshot).filter(MetricsSnapshot.context == "advisor_scorecard").count() == 1


def test_dsr_computed_once_multiple_strategy_variants_exist():
    """DSR (F11) deflates PSR against the number of strategy variants
    trialed (n_trials = count of AdvisorStrategy rows for this user) —
    stays None below 2 trials (nothing to deflate against yet)."""
    db = _memory_db()
    user = _user(db)
    portfolio = _advisor_portfolio(db, user)
    base = [0.01, -0.005, 0.008, 0.002, -0.001, 0.004, 0.006]
    returns = [round(base[i % len(base)] * (1 + 0.01 * (i % 5)), 6) for i in range(70)]
    _seed_snapshots(db, portfolio, returns)

    db.add(AdvisorStrategy(user_id=user.id, role="champion"))
    db.add(AdvisorStrategy(user_id=user.id, role="challenger"))
    db.commit()

    row = compute_advisor_scorecard(db, user.id)
    assert row is not None
    assert row.dsr is not None
    assert 0.0 <= row.dsr <= 1.0
    # DSR deflates for selection bias, so it must never exceed PSR.
    assert row.dsr <= row.psr + 1e-9
