"""4-axis advisor scorecard computation (advisor loop E1).

Once predictions mature (21-trading-day horizon), computes and persists the
four improvement axes per portfolio cohort:

1. Risk-adjusted return  — Sharpe / Sortino / Calmar of the paper book NAV.
2. Calibration           — Brier score + log-loss of confidence vs realised hit.
3. Magnitude accuracy    — Mincer-Zarnowitz slope + R² (predicted vs realised).
4. Downside discipline   — realised max drawdown + CVaR of the paper book.

Directional accuracy alone is rejected as gameable (locked decision #5). Axes
that cannot be computed yet stay NULL — honest "pending", never placeholders.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    AdvisorScorecard,
    DiscoveryPrediction,
    PaperPortfolio,
    PaperSnapshot,
)
from app.foundation.models.entities._core import now_utc
from app.decision.advisor.cycle import ADVISOR_MANDATE
from app.foundation.metrics_snapshots import upsert_metrics_snapshot
from app.foundation.quant_metrics import (
    CALENDAR_DAYS_PER_YEAR,
    calmar_ratio,
    compute_brier_and_log_loss,
    compute_bucketed_rps,
    compute_mincer_zarnowitz,
    deflated_sharpe_ratio,
    historical_cvar,
    max_drawdown,
    probabilistic_sharpe_ratio,
    resolve_n_trials,
    sharpe_ratio,
    sharpe_standard_error,
    sortino_ratio,
)
from app.foundation.settings import get_risk_free_rate

# Minimum NAV returns before a ratio is reported at all.
#
# These are honesty floors, not sufficiency thresholds — see
# `sharpe_standard_error` for why no threshold in this range makes an
# annualised Sharpe a *reliable* point estimate. They mark the point below
# which the figure stops being merely imprecise and becomes structurally
# meaningless:
#
# * Ratios (Sharpe/Sortino/Calmar) at 30. Below ~30 daily returns even the
#   *sign* is unstable, and a negative Sharpe rendered as a scorecard axis
#   reads to a human as a judgement about the strategy.
# * CVaR at 60. This one is a hard floor rather than a soft one: at 95%
#   confidence the tail holds `floor(0.05 * n) + 1` observations, so at the
#   previous n=5 guard the "expected shortfall" was a single observation and
#   was definitionally equal to VaR. 60 gives 4 — still short of the ~6 that
#   Basel's FRTB buys with a 250-day window at 97.5%, but past the point where
#   the two estimators are the same number.
MIN_RETURNS_FOR_RATIOS = 30
MIN_RETURNS_FOR_CVAR = 60


logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 90
METRICS_CONTEXT = "advisor_scorecard"


def _nav_returns(db: Session, portfolio_id: str, window_start: date, window_end: date) -> list[float]:
    snaps = (
        db.query(PaperSnapshot)
        .filter(
            PaperSnapshot.portfolio_id == portfolio_id,
            PaperSnapshot.date >= window_start,
            PaperSnapshot.date <= window_end,
        )
        .order_by(PaperSnapshot.date.asc())
        .all()
    )
    values = [float(s.total_value) for s in snaps if s.total_value is not None]
    returns: list[float] = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0:
            returns.append(cur / prev - 1.0)
    return returns


def _judged_return(p: DiscoveryPrediction) -> float:
    """Excess over the passive core when recorded, else the raw return."""
    if p.excess_return is not None:
        return float(p.excess_return)
    return float(p.realised_return or 0.0)


def compute_advisor_scorecard(
    db: Session,
    user_id: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    portfolio_id: str | None = None,
) -> AdvisorScorecard | None:
    """Compute and upsert the 4-axis scorecard for a paper sleeve.

    Defaults to the user's advisor portfolio; pass *portfolio_id* to score a
    specific sleeve (champion vs challenger, PR2 C3). Returns the persisted
    row, or ``None`` when the sleeve does not exist. Individual axes remain
    NULL when their inputs are missing (e.g. no matured predictions, too few
    snapshots).
    """
    if portfolio_id is not None:
        portfolio = (
            db.query(PaperPortfolio)
            .filter(PaperPortfolio.id == portfolio_id, PaperPortfolio.user_id == user_id)
            .one_or_none()
        )
    else:
        portfolio = (
            db.query(PaperPortfolio)
            .filter(PaperPortfolio.user_id == user_id, PaperPortfolio.mandate == ADVISOR_MANDATE)
            .one_or_none()
        )
    if portfolio is None:
        return None

    window_end = now_utc().date()
    window_start = window_end - timedelta(days=window_days)

    # Prefer sleeve-attributed predictions (PR2); fall back to the legacy
    # user-wide window only when the sleeve has no attributed rows yet.
    pred_window = DiscoveryPrediction.predicted_at >= now_utc() - timedelta(days=window_days)
    preds = (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.portfolio_id == portfolio.id,
            pred_window,
        )
        .all()
    )
    prediction_scope = "portfolio"
    if not preds:
        preds = (
            db.query(DiscoveryPrediction)
            .filter(DiscoveryPrediction.user_id == user_id, pred_window)
            .all()
        )
        prediction_scope = "user_legacy"
    resolved = [p for p in preds if p.outcome_status == "resolved" and p.realised_return is not None]

    # Axis 2 — calibration. Directional only: a logged hold ("neutral") is a
    # magnitude forecast with no directional bet, so scoring its confidence as
    # a directional hit would be meaningless. Neutrals still feed axis 3.
    directional = [p for p in resolved if p.direction != "neutral"]
    # A hit is beating the passive core over the same window (Phase 2); rows
    # resolved before the benchmark was recorded fall back to the raw move.
    calib_pairs = [
        (float(p.conviction), 1 if _judged_return(p) > 0 else 0)
        for p in directional
        if p.conviction is not None
    ]
    brier_avg, log_loss_avg = compute_brier_and_log_loss(calib_pairs)

    # F11: bucketed RPS supersedes brier_avg for the composite's calibration
    # axis — sign-only Brier never checked whether the predicted *magnitude*
    # distribution (p5/p95 band) was calibrated, only the direction. Applies
    # to all resolved predictions with an MC band, including neutrals (a
    # "hold" still carries a forward distribution).
    rps_triples = [
        (float(p.expected_return_low), float(p.expected_return_high), float(p.realised_return))
        for p in resolved
        if p.expected_return_low is not None
        and p.expected_return_high is not None
        and p.realised_return is not None
    ]
    rps_avg = compute_bucketed_rps(rps_triples)

    # Axis 3 — magnitude accuracy (needs a predicted magnitude)
    mz_pairs = [
        (float(p.expected_return), float(p.realised_return))
        for p in resolved
        if p.expected_return is not None and p.realised_return is not None
    ]
    mz_slope, mz_r2 = compute_mincer_zarnowitz(mz_pairs)

    # Axes 1 + 4 — paper book NAV over the window
    returns = _nav_returns(db, portfolio.id, window_start, window_end)
    sharpe = sortino = calmar = mdd = cvar = sharpe_se = psr = dsr = None
    if len(returns) >= MIN_RETURNS_FOR_RATIOS:
        # PaperSnapshot rows are written daily including weekends, so this NAV
        # series is calendar-cadence — the same series paper_portfolio.py
        # annualises at CALENDAR_DAYS_PER_YEAR. Leaving the 252 default here
        # made the *same* paper portfolio report two Sharpes differing by
        # sqrt(365/252) (~20%), and both land in metrics_snapshots under the
        # same portfolio_id, differing only by `context`.
        sharpe = float(sharpe_ratio(returns, periods_per_year=CALENDAR_DAYS_PER_YEAR))
        sortino = float(sortino_ratio(returns, periods_per_year=CALENDAR_DAYS_PER_YEAR))
        calmar = float(calmar_ratio(returns, periods_per_year=CALENDAR_DAYS_PER_YEAR))
        mdd = abs(float(max_drawdown(returns).get("max_drawdown", 0.0)))
        sharpe_se = float(
            sharpe_standard_error(sharpe, len(returns), periods_per_year=CALENDAR_DAYS_PER_YEAR)
        )
        # PSR/DSR (F11): probability the *true* Sharpe beats cash's own
        # (zero-excess) Sharpe, accounting for sample size, skew, and
        # kurtosis — unlike sigmoid(sharpe), which treats a short, noisy
        # window's raw point estimate as if it were already skill-adjusted.
        # sr_benchmark defaults to 0.0 (an annualised Sharpe threshold);
        # since `returns` are risk-free-adjusted internally via `risk_free`,
        # testing against 0 excess Sharpe *is* "benchmark against cash".
        risk_free = get_risk_free_rate(db)
        psr = float(
            probabilistic_sharpe_ratio(
                returns, risk_free=risk_free, periods_per_year=CALENDAR_DAYS_PER_YEAR
            )
        )
        # n_trials = the global ledger-backed trial count (Finding F15, ADR
        # 0015) — counting only this user's AdvisorStrategy rows ran 2-6,
        # making the deflation a rounding error; resolve_n_trials() counts the
        # true search breadth across every context.
        n_trials = resolve_n_trials(db)
        dsr = float(
            deflated_sharpe_ratio(
                returns, n_trials, risk_free=risk_free, periods_per_year=CALENDAR_DAYS_PER_YEAR
            )
        )
    # A tail estimator needs far more data than a moment estimator, so it gets
    # its own, higher floor rather than riding along with the ratios above.
    if len(returns) >= MIN_RETURNS_FOR_CVAR:
        cvar = float(historical_cvar(returns, confidence=0.95))

    details: dict[str, Any] = {
        "window_days": window_days,
        "n_calibration_pairs": len(calib_pairs),
        "n_rps_triples": len(rps_triples),
        "n_magnitude_pairs": len(mz_pairs),
        "n_directional": len(directional),
        "n_neutral": len(resolved) - len(directional),
        "n_nav_returns": len(returns),
        # Lo (2002) SE of the annualised Sharpe. Rendered as a band by the UI;
        # `sharpe_ci_spans_zero` is the "do not rank on this" flag.
        "sharpe_se": sharpe_se,
        "sharpe_ci_spans_zero": (
            None if sharpe is None or sharpe_se is None else abs(sharpe) < 1.96 * sharpe_se
        ),
        "min_returns_for_ratios": MIN_RETURNS_FOR_RATIOS,
        "min_returns_for_cvar": MIN_RETURNS_FOR_CVAR,
        "prediction_scope": prediction_scope,
    }

    row = (
        db.query(AdvisorScorecard)
        .filter(
            AdvisorScorecard.portfolio_id == portfolio.id,
            AdvisorScorecard.window_end == window_end,
        )
        .one_or_none()
    )
    if row is None:
        row = AdvisorScorecard(
            user_id=user_id,
            portfolio_id=portfolio.id,
            window_start=window_start,
            window_end=window_end,
        )
        db.add(row)

    row.window_start = window_start
    row.n_predictions = len(preds)
    row.n_resolved = len(resolved)
    row.sharpe = sharpe
    row.sortino = sortino
    row.calmar = calmar
    row.psr = psr
    row.dsr = dsr
    row.brier_avg = brier_avg
    row.log_loss_avg = log_loss_avg
    row.rps_avg = rps_avg
    row.mz_slope = mz_slope
    row.mz_r2 = mz_r2
    row.max_drawdown = mdd
    row.cvar_95 = cvar
    row.details_json = details
    row.computed_at = now_utc()

    upsert_metrics_snapshot(
        db,
        portfolio_id=portfolio.id,
        as_of=window_end,
        context=METRICS_CONTEXT,
        values={
            "sharpe": sharpe,
            "sortino": sortino,
            "calmar": calmar,
            "max_drawdown": mdd,
            "cvar_95": cvar,
        },
    )

    db.commit()
    db.refresh(row)
    logger.info(
        "advisor scorecard for %s: n_resolved=%d brier=%s mz_slope=%s sharpe=%s",
        portfolio.id, len(resolved), brier_avg, mz_slope, sharpe,
    )
    return row
