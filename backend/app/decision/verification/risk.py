"""Portfolio risk for Portfolio -> Risk (VaR, drawdown, concentration, currency look-through).

Return-based numbers (VaR, drawdown, Sharpe/Sortino) come from the daily returns
of the user's *current* holdings, weighted by position value and taken from
price history (``PortfolioPriceService``, the same series Quant Lab's Risk view
uses). They are never derived from ``PortfolioSnapshot.total_value``: that
number is cash + securities + manual holdings, so a salary payment or a
purchase reads as a return and inflates every risk figure.

Holdings-based numbers (concentration, look-through HHI, currency exposure) come
from the holdings themselves; index ETFs are looked through to the currencies
their index trades in (``foundation.etf_currency``, ADR 0007 amendment 4).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.decision.verification.alerts import RiskAlert, check_concentration_alerts, get_alert_thresholds
from app.foundation.models.entities import Holding, Portfolio
from app.foundation.etf_currency import exposure_for_holding
from app.foundation.etf_lookthrough import portfolio_lookthrough
from app.foundation.portfolio_price_service import PortfolioPriceService
from app.foundation.portfolio_utils import holding_market_value
from app.foundation.settings import get_risk_free_rate

logger = logging.getLogger(__name__)

# Trading days of history the return-based metrics look at (about one year).
MAX_RETURN_OBSERVATIONS = 260
# Below this many daily returns the ratios are suppressed, not shown.
MIN_OBSERVATIONS = 30


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ReturnBasis:
    """What the return-based numbers were computed from (shown next to them)."""

    source: str = "holdings_price_history"
    n_obs: int = 0
    start: str | None = None
    end: str | None = None
    priced_assets: list[str] = field(default_factory=list)
    missing_history: list[str] = field(default_factory=list)
    message: str | None = None


@dataclass
class RiskAssessment:
    """Full risk snapshot for a portfolio."""

    var_95: float = 0.0
    var_99: float = 0.0
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    concentration_index: float = 0.0  # HHI (0–1)
    lookthrough_hhi: float = 0.0  # effective HHI considering ETF constituents
    lookthrough_count: int = 0  # total underlying positions
    asset_type_exposure: dict[str, float] = field(default_factory=dict)
    currency_exposure: dict[str, float] = field(default_factory=dict)
    alerts: list[RiskAlert] = field(default_factory=list)
    insufficient_history: bool = False  # True when < MIN_OBSERVATIONS returns; ratios are 0.0
    return_basis: ReturnBasis = field(default_factory=ReturnBasis)
    # Index currency weights used for look-through that are past
    # etf_currency.STALE_AFTER (internal; surfaced as an alert).
    stale_currency_weights: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helper functions (numpy-free, consistent with quant_metrics.py style)
# ---------------------------------------------------------------------------

def portfolio_return_series(
    db: Session, user_id: str, *, max_obs: int = MAX_RETURN_OBSERVATIONS
) -> tuple[list[float], ReturnBasis]:
    """Daily returns of the user's current holdings, weighted by position value.

    Uses the strict date overlap of every priced holding (no forward fill), so
    a holding with a short price history shortens the window rather than
    inventing returns. Returns ``([], basis)`` when nothing can be priced.
    """
    basis = ReturnBasis()
    try:
        service = PortfolioPriceService(db, user_id)
        matrix, weights, diagnostics = service.price_matrix()
        basis.priced_assets = list(diagnostics.get("priced_assets", []))
        basis.missing_history = list(diagnostics.get("missing_history", []))
        basis.message = diagnostics.get("message")
        if not matrix:
            return [], basis
        series = service.weighted_returns(matrix, weights).tail(max_obs)
    except Exception:
        logger.warning("risk: portfolio return series unavailable for user %s", user_id, exc_info=True)
        basis.message = "Price history is unavailable right now."
        return [], basis
    returns = [float(v) for v in series.tolist()]
    if returns:
        basis.n_obs = len(returns)
        basis.start = str(series.index[0])[:10]
        basis.end = str(series.index[-1])[:10]
    return returns, basis


def _historical_var(returns: Sequence[float], confidence: float) -> float:
    """Historical Value-at-Risk (loss magnitude at *confidence* level).

    Delegates to the canonical :func:`quant_metrics.historical_var`
    (ADR 0003, decision 1), which applies the same None-filtering via its
    ``_to_list`` helper. Convention note (documented change): series with
    fewer than two observations now return 0.0 — a lone data point is not
    a distribution tail.
    """
    from app.foundation.quant_metrics import historical_var as _canonical_historical_var

    return _canonical_historical_var(returns, confidence)


def _sharpe(returns: Sequence[float], risk_free: float = 0.0, min_observations: int = MIN_OBSERVATIONS) -> float:
    """Annualised Sharpe ratio — delegates to quant_metrics for consistency.

    Returns 0.0 (rather than a misleading ratio) when the sample is shorter
    than *min_observations* (default 30 trading days ≈ 6 weeks).
    """
    rs = [float(r) for r in returns if r is not None]
    if len(rs) < min_observations:
        return 0.0
    from app.foundation.quant_metrics import sharpe_ratio

    return sharpe_ratio(rs, risk_free=risk_free)


def _sortino(returns: Sequence[float], risk_free: float = 0.0, min_observations: int = MIN_OBSERVATIONS) -> float:
    """Annualised Sortino ratio — same *min_observations* guard as :func:`_sharpe`."""
    rs = [float(r) for r in returns if r is not None]
    if len(rs) < min_observations:
        return 0.0
    from app.foundation.quant_metrics import sortino_ratio

    return sortino_ratio(rs, risk_free=risk_free)


def _drawdown_series(returns: Sequence[float]) -> tuple[float, float]:
    """Return (max_drawdown, current_drawdown) both as negative fractions.

    ``max_drawdown`` delegates to ``quant_metrics.max_drawdown`` — the same
    source every other consumer uses — so this module reports the identical
    number a user would see elsewhere for the same return series.
    ``current_drawdown`` (the drawdown as of the *last* observation, not
    necessarily the deepest) has no `quant_metrics` equivalent and stays local.
    """
    rs = [float(r) for r in returns if r is not None]
    if not rs:
        return 0.0, 0.0
    from app.foundation.quant_metrics import max_drawdown as _qm_max_drawdown

    max_dd = _qm_max_drawdown(rs)["max_drawdown"]
    equity = 1.0
    peak = 1.0
    for r in rs:
        equity *= 1 + r
        if equity > peak:
            peak = equity
    current_dd = (equity / peak) - 1
    return max_dd, current_dd


def _hhi(weights: Sequence[float]) -> float:
    """Herfindahl–Hirschman Index over position weights (0 = perfectly diversified, 1 = single asset)."""
    ws = [abs(float(w)) for w in weights if w is not None]
    if not ws:
        return 0.0
    total = sum(ws)
    if total == 0:
        return 0.0
    normed = [w / total for w in ws]
    return sum(w ** 2 for w in normed)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def compute_risk_metrics(portfolio_id: str, db: Session) -> RiskAssessment:
    """Compute a full risk assessment for *portfolio_id*.

    VaR, drawdown and Sharpe/Sortino come from :func:`portfolio_return_series`;
    concentration and currency exposure from the portfolio's holdings.
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return RiskAssessment()

    # --- return-based metrics (current holdings, price history) -------------
    returns, basis = portfolio_return_series(db, portfolio.user_id)
    var_95 = _historical_var(returns, 0.95) if returns else 0.0
    var_99 = _historical_var(returns, 0.99) if returns else 0.0
    max_dd, current_dd = _drawdown_series(returns)

    insufficient_history = len(returns) < MIN_OBSERVATIONS
    risk_free = get_risk_free_rate(db)
    sharpe = _sharpe(returns, risk_free=risk_free)
    sortino = _sortino(returns, risk_free=risk_free)

    # --- holdings-based metrics ---------------------------------------------
    holdings: list[Holding] = (
        db.query(Holding)
        .filter(Holding.portfolio_id == portfolio_id)
        .all()
    )

    weights: list[float] = []
    asset_type_map: dict[str, float] = {}
    currency_map: dict[str, float] = {}
    stale_weights: set[str] = set()
    total_value = 0.0
    for h in holdings:
        val = holding_market_value(db, h)
        total_value += val
        weights.append(val)
        asset_type_map[h.asset_type] = asset_type_map.get(h.asset_type, 0.0) + val
        # The currencies the value moves with: the priced-in currency for a
        # share (DKB books an AAPL position in EUR, but it is a dollar
        # exposure), and the index's constituents for a mapped index ETF
        # (EUNL.DE is quoted in EUR; ~73% of MSCI World trades in USD).
        exposure = exposure_for_holding(db, h)
        for ccy, share in exposure.weights.items():
            currency_map[ccy] = currency_map.get(ccy, 0.0) + val * share
        if exposure.stale and exposure.index_key:
            stale_weights.add(f"{exposure.index_key} (as of {exposure.as_of})")

    concentration = _hhi(weights)

    lookthrough = portfolio_lookthrough(db, portfolio.user_id, portfolio_id=portfolio.id)
    lookthrough_hhi = lookthrough.get("effective_hhi", 0.0)
    lookthrough_count = lookthrough.get("lookthrough_count", 0)

    asset_type_exposure = {k: v / total_value for k, v in asset_type_map.items()} if total_value else {}
    currency_exposure = {k: v / total_value for k, v in currency_map.items()} if total_value else {}

    return RiskAssessment(
        var_95=var_95,
        var_99=var_99,
        max_drawdown=max_dd,
        current_drawdown=current_dd,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        concentration_index=concentration,
        lookthrough_hhi=lookthrough_hhi,
        lookthrough_count=lookthrough_count,
        asset_type_exposure=asset_type_exposure,
        currency_exposure=currency_exposure,
        alerts=[],  # populated by check_risk_alerts
        insufficient_history=insufficient_history,
        return_basis=basis,
        stale_currency_weights=sorted(stale_weights),
    )


def check_risk_alerts(
    portfolio_id: str, db: Session, assessment: RiskAssessment | None = None
) -> list[RiskAlert]:
    """Generate actionable risk alerts for *portfolio_id*.

    Pass an already computed *assessment* to avoid a second pass over prices.
    Drawdown thresholds are the ``verification_drawdown_*`` settings. Holding-
    level concentration is judged by the single-name alerts in
    ``alerts.check_concentration_alerts`` (a 60 % MSCI World ETF is not
    concentration risk), so it is deliberately absent here.
    """
    assessment = assessment or compute_risk_metrics(portfolio_id, db)
    thresholds = get_alert_thresholds(db)
    alerts: list[RiskAlert] = []

    # --- drawdown alerts (current holdings, price history) ------------------
    window = assessment.return_basis.n_obs
    if assessment.current_drawdown <= thresholds.drawdown_critical:
        alerts.append(RiskAlert(
            type="drawdown",
            severity="critical",
            title="Deep drawdown in progress",
            message=(
                f"Your current holdings are {abs(assessment.current_drawdown):.1%} below their "
                f"peak over the last {window} trading days."
            ),
        ))
    elif assessment.current_drawdown <= thresholds.drawdown_warning:
        alerts.append(RiskAlert(
            type="drawdown",
            severity="warning",
            title="Elevated drawdown",
            message=(
                f"Your current holdings are {abs(assessment.current_drawdown):.1%} below their "
                f"peak over the last {window} trading days."
            ),
        ))

    # --- look-through diversification info ----------------------------------
    if assessment.lookthrough_count > 0 and assessment.lookthrough_hhi < assessment.concentration_index:
        portfolio = db.get(Portfolio, portfolio_id)
        if portfolio is not None:
            lookthrough_data = portfolio_lookthrough(db, portfolio.user_id, portfolio_id=portfolio.id)
            etf_count = lookthrough_data.get("etf_holdings_count", 0)
            if etf_count > 0:
                alerts.append(RiskAlert(
                    type="concentration",
                    severity="info",
                    title="Effective diversification via ETFs",
                    message=(
                        f"Your portfolio shows {assessment.concentration_index:.2f} HHI at the holding level, "
                        f"but look-through analysis reveals ~{assessment.lookthrough_count} underlying positions "
                        f"with an effective HHI of {assessment.lookthrough_hhi:.2f}. "
                        f"Your {etf_count} ETF position{'s' if etf_count > 1 else ''} significantly reduce "
                        f"concentration risk."
                    ),
                ))

    # --- currency exposure alerts -------------------------------------------
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is not None:
        base = portfolio.currency
        for cur, pct in assessment.currency_exposure.items():
            if cur != base and pct > 0.30:
                alerts.append(RiskAlert(
                    type="currency",
                    severity="warning",
                    title=f"Foreign currency exposure: {cur}",
                    message=(
                        f"{pct:.0%} of your portfolio moves with {cur} "
                        f"(base currency: {base}), counting the currencies "
                        f"inside index ETFs.  Currency fluctuations may "
                        f"impact your returns."
                    ),
                ))
        if assessment.stale_currency_weights:
            alerts.append(RiskAlert(
                type="currency",
                severity="warning",
                title="Currency look-through weights are out of date",
                message=(
                    "The index currency weights used for your ETFs are over six "
                    "months old: " + ", ".join(assessment.stale_currency_weights)
                    + ".  The monthly refresh has not succeeded since; the "
                    "currency exposure above may have drifted."
                ),
            ))

    return alerts


def risk_assessment_with_alerts(portfolio_id: str, db: Session) -> tuple[RiskAssessment, list[RiskAlert]]:
    """One pass: the assessment plus every alert the Risk page shows.

    Drawdown/currency/look-through alerts from :func:`check_risk_alerts` and the
    single-name concentration alerts from ``alerts.check_concentration_alerts``,
    critical first.
    """
    assessment = compute_risk_metrics(portfolio_id, db)
    alerts: list[Any] = list(check_risk_alerts(portfolio_id, db, assessment))
    alerts.extend(check_concentration_alerts(portfolio_id, db))
    order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a.severity, 3))
    assessment.alerts = alerts
    return assessment, alerts
