"""Unified expected-return anchor selector, composing instrument taxonomy
(``app.foundation.instrument_taxonomy``, Phase 1) with the metrics module's
annualisation convention (``app.foundation.quant_metrics``, Phase 2).

Every value :func:`select_return_anchor` returns is a BACKWARD-LOOKING
realised return, never a forecast — callers must present it as such. The
forward estimate is built from it further down: :func:`equilibrium_prior`
plus a :func:`credibility_weight` share of the anchor's gap to that prior
(ADR 0017) (see the dossier_writer.py
"critique the anchor" prompt for how Discover surfaces this distinction to
the LLM and the user). See docs/archive/plans/unified-portfolio-engine-implementation.md
Phase 3.

Money-market/cash-equivalent instruments carry no duration risk and reprice
with the current policy rate almost immediately, so a multi-year trailing
CAGR can blend several rate regimes and mislead (the XEON.DE incident: 3y
CAGR 3.0% vs. ~2.1% implied by the then-current €STR). These instead prefer
a short trailing window. Equity/ETF instruments prefer the long trailing
window (whatever span the caller computed it over — Discover's convention is
3y), falling back to 12-1 month momentum when unavailable. Bond funds
(duration-bearing fixed-income) mirror that equity/ETF priority: duration
still compounds over multi-year spans, so the long trailing window leads
(discover-maths audit O4).
"""
from __future__ import annotations

from typing import Any, TypedDict

from app.foundation.instrument_taxonomy import InstrumentType


class ReturnAnchor(TypedDict):
    value: float
    method: str
    horizon: str


# Selection priority per instrument type: ordered list of (candidate key,
# horizon label). The first candidate with a non-None value wins.
_ANCHOR_PRIORITY: dict[str, list[tuple[str, str]]] = {
    "money_market": [
        ("trailing_1m_annualized_return", "1m"),
        ("momentum_12_1m", "12m"),
    ],
    "etf": [
        ("trailing_3y_annualized_return", "3y"),
        ("momentum_12_1m", "12m"),
    ],
    "bond": [
        ("trailing_3y_annualized_return", "3y"),
        ("momentum_12_1m", "12m"),
    ],
    "equity": [
        ("trailing_3y_annualized_return", "3y"),
        ("momentum_12_1m", "12m"),
    ],
}


def select_return_anchor(
    instrument_type: InstrumentType | str,
    candidates: dict[str, float | None] | Any,
) -> ReturnAnchor | None:
    """Pick the instrument-type-appropriate realised-return anchor.

    ``candidates`` maps method name (e.g. ``"trailing_3y_annualized_return"``,
    ``"trailing_1m_annualized_return"``, ``"momentum_12_1m"``) to an
    annualised return fraction (0.03 == 3%) or ``None`` when that signal
    wasn't computable for this candidate. Returns ``None`` (no anchor
    available at all) rather than 0.0 when nothing in the priority list has
    a value — callers must not treat "no data" as "flat return".
    """
    if not isinstance(candidates, dict):
        return None
    priority = _ANCHOR_PRIORITY.get(str(instrument_type), _ANCHOR_PRIORITY["equity"])
    for method, horizon in priority:
        value = candidates.get(method)
        if value is not None:
            return {"value": float(value), "method": method, "horizon": horizon}
    return None



# ---------------------------------------------------------------------------
# Market-implied prior + credibility tilt (ADR 0017)
# ---------------------------------------------------------------------------

# Expected equity return over cash, annual. Refreshed annually; last set
# 2026-09-26, next due January 2027. Cross-checked against three sources:
#  - UBS Global Investment Returns Yearbook 2026 (Dimson/Marsh/Staunton):
#    world equity premium over bills ~3.5% geometric, ~5% arithmetic;
#  - Damodaran's implied ERP: 4.23% over US T-bonds at the start of 2026,
#    4.17% mature-market in July 2026 (about 0.5pp more over cash);
#  - Kroll's recommended eurozone ERP: 5.0-5.5% (January/March 2026).
# 4.5% sits inside that range. It pairs with GRAND_MEAN_ANNUAL["equity"]
# (7%) in expected_return_blocks at a ~2.5% cash rate.
EQUITY_PREMIUM_OVER_CASH = 0.045

# Cross-sectional dispersion (sd) of true expected returns among the large
# caps Discover screens. CAPM alone implies ERP x sd(beta) ~ 4.5% x 0.35 ~
# 1.6%; value/profitability/momentum premia roughly double that. Lewellen
# (2015) finds 0.87%/month with 15 characteristics, but his sample is
# dominated by small caps and the dispersion comes from characteristics, not
# from trailing returns, which carry little to no forward information at a
# 12-month horizon (Goyal & Welch 2008; De Bondt & Thaler 1985).
EXPECTED_RETURN_DISPERSION = 0.03

# Beta is clamped to this range before the Blume adjustment: a regression
# on a short or illiquid history can print nonsense (negative, or 4+).
_BETA_CLAMP = (0.0, 2.5)


def blume_adjusted_beta(raw_beta: float) -> float:
    """Blume (1971) adjustment toward 1: ``2/3 * beta + 1/3``.

    Betas regress toward the cross-sectional mean of 1 across estimation
    periods, so the raw OLS estimate is a biased forecast of next year's
    beta. Only meaningful for equities and equity funds, where the
    cross-sectional mean really is ~1.
    """
    clamped = max(_BETA_CLAMP[0], min(_BETA_CLAMP[1], float(raw_beta)))
    return (2.0 / 3.0) * clamped + (1.0 / 3.0)


def equilibrium_prior(
    instrument_type: InstrumentType | str,
    risk_free: float,
    beta: float | None,
) -> tuple[float, float | None]:
    """Market-implied annual expected return: ``rf + beta * premium``.

    Returns ``(prior, beta_used)``. This is what an investor should expect
    from the instrument with no stock-specific view, the same equilibrium
    starting point Black-Litterman uses (He & Litterman 1999; Pastor &
    Stambaugh 1999 shrink toward it too). Trailing returns used to be shrunk
    toward 0%, which put 6 of 15 LONG picks below the ECB deposit rate
    (BBVA.MC audit, 2026-09-26).

    - money_market: ``rf`` (no market exposure; beta ignored).
    - bond: ``rf + max(0, beta) * premium`` with the raw beta. Blume's
      pull toward 1 would invent equity risk.
    - equity/etf/other: ``rf + blume(beta) * premium``; a missing beta
      counts as the market's 1.0.

    ``risk_free`` is the EUR cash rate even for USD listings: under
    uncovered interest parity a EUR investor's expected return on a USD
    asset is the EUR rate plus the asset's premium.
    """
    kind = str(instrument_type)
    if kind == "money_market":
        return risk_free, None
    if kind == "bond":
        b = max(0.0, min(_BETA_CLAMP[1], float(beta))) if beta is not None else 0.0
        return risk_free + b * EQUITY_PREMIUM_OVER_CASH, b
    b = blume_adjusted_beta(beta) if beta is not None else 1.0
    return risk_free + b * EQUITY_PREMIUM_OVER_CASH, b


def credibility_weight(
    volatility_annual: float | None,
    window_years: float | None,
    cap: float,
    dispersion: float = EXPECTED_RETURN_DISPERSION,
) -> float:
    """How much of a trailing return's gap to the prior to believe.

    Bühlmann credibility / normal-normal Bayes: the trailing mean return
    over ``T`` years estimates the true expected return with sampling
    variance ``vol**2 / T``, around a prior with cross-sectional variance
    ``dispersion**2``, so the posterior puts weight
    ``d**2 / (d**2 + vol**2 / T)`` on the data. A 3-year record of a stock
    with 28% volatility earns ~3%, one with 15% volatility ~10%. Never more
    than *cap* (the per-class constant, or the fitted Mincer-Zarnowitz slope
    once enough outcomes have resolved). Falls back to *cap* when volatility
    or window is unknown, e.g. for stored scores from before these fields
    existed.
    """
    if volatility_annual is None or window_years is None:
        return cap
    try:
        vol = float(volatility_annual)
        t = float(window_years)
    except (TypeError, ValueError):
        return cap
    if vol <= 0 or t <= 0:
        return cap
    d2 = dispersion ** 2
    weight = d2 / (d2 + vol * vol / t)
    return max(0.0, min(cap, weight))
