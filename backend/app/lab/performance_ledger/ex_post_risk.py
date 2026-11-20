"""Ex-post risk metrics for performance ledger."""

from dataclasses import dataclass, asdict
from app.foundation import quant_metrics


@dataclass
class ExPostRiskMetrics:
    """Ex-post risk metrics for a composite or portfolio."""
    volatility: float
    sharpe: float
    sortino: float
    downside_volatility: float
    max_drawdown: float
    drawdown_duration_days: int
    calmar: float
    cvar_95: float
    skewness: float
    kurtosis: float


def calculate_ex_post_risk(
    returns: list[float],
    benchmark_returns: list[float] | None = None,
    risk_free_rate: float = 0.0,
    periods_per_year: int = quant_metrics.TRADING_DAYS_PER_YEAR,
    confidence: float = 0.95,
) -> ExPostRiskMetrics:
    """
    Calculate comprehensive ex-post risk metrics.

    Annualisation convention: trading days by default
    (``quant_metrics.TRADING_DAYS_PER_YEAR``); every annualised metric threads
    ``periods_per_year`` through to the canonical ``quant_metrics`` primitives.

    Reconciliation note (ADR ``docs/adr/0003-quant-metrics-conventions.md``
    decision 4, audit §10 matrix #2): this bundle and
    ``quant_metrics.full_risk_report`` overlap heavily but are KEPT DISTINCT on
    purpose — parameterize, don't flatten. They disagree numerically in three
    documented places: skewness/kurtosis here are raw scipy moments (biased,
    guards n>2/n>3) while ``full_risk_report`` uses bias-corrected G1/G2;
    ``downside_volatility`` here is the annualised volatility of negative-only
    returns while ``full_risk_report`` reports an unannualised sample
    semi-deviation; and the output shapes differ (flat dataclass vs dict with
    an embedded drawdown dict). Do NOT merge the two without a decision that
    explicitly moves those published numbers.

    Args:
        returns: Daily returns array
        benchmark_returns: Optional benchmark returns for comparison
        risk_free_rate: Annual risk-free rate (default 0%)
        periods_per_year: Trading days (default 252)
        confidence: CVaR confidence level (default 0.95, the historical
            hardcoded value)

    Returns:
        ExPostRiskMetrics with all risk measures
    """
    if not returns or len(returns) < 2:
        return ExPostRiskMetrics(
            volatility=0.0,
            sharpe=0.0,
            sortino=0.0,
            downside_volatility=0.0,
            max_drawdown=0.0,
            drawdown_duration_days=0,
            calmar=0.0,
            cvar_95=0.0,
            skewness=0.0,
            kurtosis=0.0,
        )

    # Use quant_metrics for calculations (existing reusable functions)
    volatility = quant_metrics.annualised_volatility(returns, periods_per_year=periods_per_year)
    sharpe = quant_metrics.sharpe_ratio(returns, risk_free=risk_free_rate, periods_per_year=periods_per_year)
    sortino = quant_metrics.sortino_ratio(returns, risk_free=risk_free_rate, periods_per_year=periods_per_year)

    drawdown_info = quant_metrics.max_drawdown(returns)
    max_dd = drawdown_info.get("max_drawdown", 0.0)
    drawdown_duration = drawdown_info.get("max_drawdown_duration", 0)

    calmar = quant_metrics.calmar_ratio(returns, periods_per_year=periods_per_year)
    cvar = quant_metrics.historical_cvar(returns, confidence=confidence)

    # Calculate downside volatility for Sortino denominator
    downside_returns = [r for r in returns if r < 0]
    downside_vol = (
        quant_metrics.annualised_volatility(downside_returns, periods_per_year=periods_per_year)
        if downside_returns
        else 0.0
    )

    # Calculate skewness and kurtosis
    from scipy import stats

    skew = float(stats.skew(returns)) if len(returns) > 2 else 0.0
    kurt = float(stats.kurtosis(returns)) if len(returns) > 3 else 0.0

    return ExPostRiskMetrics(
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        downside_volatility=downside_vol,
        max_drawdown=max_dd,
        drawdown_duration_days=int(drawdown_duration),
        calmar=calmar,
        cvar_95=cvar,
        skewness=skew,
        kurtosis=kurt,
    )


def ex_post_risk_to_dict(metrics: ExPostRiskMetrics) -> dict:
    """Convert ExPostRiskMetrics to JSON-serializable dict."""
    return asdict(metrics)
