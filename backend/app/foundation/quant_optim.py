"""skfolio-powered portfolio optimisation and stress-testing service.

Replaces the homegrown numpy optimisers in quant.py:296–353 with the full
skfolio estimator catalogue.  Old functions remain in quant.py for back-compat.
"""
from __future__ import annotations

from typing import Any

from app.foundation.quant import price_matrix_to_returns
from app.foundation.quant_metrics import TRADING_DAYS_PER_YEAR

_RISK_MEASURE_MAP = {
    "variance": "VARIANCE",
    "semi_variance": "SEMI_VARIANCE",
    "cvar": "CVAR",
    "evar": "EVAR",
    "cdar": "CDAR",
    "ulcer": "ULCER_INDEX",
    "worst": "WORST_REALIZATION",
}

_OBJECTIVE_MAP = {
    "min_risk": "MINIMIZE_RISK",
    "max_return": "MAXIMIZE_RETURN",
    "max_utility": "MAXIMIZE_UTILITY",
    "max_ratio": "MAXIMIZE_RATIO",
}

_CV_MAP = {
    "walkforward": "WalkForward",
    "combinatorial": "CombinatorialPurgedCV",
    "randomized": "MultipleRandomizedCV",
}

# Policy weights + tactical bands by instrument_type (F8/F10/F13). A 13-point
# risk_free_rate sweep on the live 499-day return matrix proved rf alone
# cannot fix the cash-concentration failure — between 25bp of rf the
# allocation swings from 74% cash to 0% cash, with no interior value that
# gives a sane result. skfolio's MAXIMIZE_RATIO objective has no concept of
# "sane" asset-class balance on its own; without a class-level floor/ceiling,
# a near-zero-vol money-market instrument's Sharpe-style ratio structurally
# dominates. "cash" here means money_market. Moderate-growth profile, per
# user decision.
POLICY_WEIGHTS: dict[str, float] = {"equity": 0.50, "bond": 0.35, "cash": 0.15}
TACTICAL_BAND = 0.10

# instrument_type -> policy group label. "etf" defaults to the equity/growth
# sleeve (a diversified-equity-basket ETF, since bond and money_market are
# now distinct instrument_types of their own — see instrument_taxonomy.py).
POLICY_GROUP: dict[str, str] = {
    "equity": "equity",
    "etf": "equity",
    "bond": "bond",
    "money_market": "cash",
}


def _build_policy_constraints(
    instrument_types: dict[str, str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Build skfolio ``groups``/``linear_constraints`` from per-ticker
    instrument types, enforcing POLICY_WEIGHTS +/- TACTICAL_BAND per class.

    An unrecognised instrument_type fails open to the "equity" group, same
    as classify_instrument's own fail-open default.
    """
    groups = {
        ticker: [POLICY_GROUP.get(itype, "equity")]
        for ticker, itype in instrument_types.items()
    }
    # Only constrain groups actually present among the assets: skfolio warns
    # (and drops the constraint) for a group referenced by linear_constraints
    # but absent from groups, and — more importantly — if a whole policy
    # sleeve has zero assets, its target weight has nowhere to go, so the
    # *other* sleeves' bands must be renormalised to still be able to sum to
    # the full 1.0 budget (e.g. equity in [0.40,0.60] + cash in [0.05,0.25]
    # can never reach 1.0 on their own if there's no bond sleeve to fill the
    # rest — infeasible without this).
    present_groups = {g for labels in groups.values() for g in labels}
    present_policy_groups = [g for g in POLICY_WEIGHTS if g in present_groups]
    if not present_policy_groups:
        return groups, []
    present_total = sum(POLICY_WEIGHTS[g] for g in present_policy_groups)

    constraints: list[str] = []
    for group in present_policy_groups:
        target = POLICY_WEIGHTS[group] / present_total
        lo = max(0.0, target - TACTICAL_BAND)
        hi = min(1.0, target + TACTICAL_BAND)
        constraints.append(f"{group} <= {hi}")
        constraints.append(f"{group} >= {lo}")
    return groups, constraints


def per_period_risk_free_rate(
    annual_rate: float | None,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Convert an annual risk-free rate to the per-period rate of the returns.

    skfolio's MeanRisk works in the units of the return matrix it is fitted
    on — daily returns here — so its ``risk_free_rate`` must be a daily rate
    too. Feeding it the annual fraction (e.g. 0.022) puts the hurdle ~250x
    too high: every asset's expected daily return sits below it and
    MAXIMIZE_RATIO refuses to solve, which is what silently sent the advisor
    proposal to its equal-weight fallback on every cycle.
    """
    if not annual_rate:
        return 0.0
    return (1.0 + float(annual_rate)) ** (1.0 / periods_per_year) - 1.0


def _returns_from_price_matrix(price_matrix: dict[str, dict[str, float]]) -> Any:
    # Delegates to the shared, non-forward-filling converter in quant.py --
    # see price_matrix_to_returns() for why ffill-before-pct_change is wrong.
    return price_matrix_to_returns(price_matrix)


def run_optimisation(
    price_matrix: dict[str, dict[str, float]],
    objective: str = "max_ratio",
    risk_measure: str = "cvar",
    cv_scheme: str | None = None,
    lookback_days: int = 730,
    weights_constraint: dict[str, Any] | None = None,
    regime_context: Any | None = None,
    risk_free_rate: float | None = None,
    instrument_types: dict[str, str] | None = None,
    previous_weights: dict[str, float] | None = None,
    max_turnover: float | None = None,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> dict[str, Any]:
    """Run a full skfolio optimisation on a price matrix.

    Args:
        price_matrix: {ticker: {date_str: price}} dict.
        objective: Optimization objective (min_risk/max_return/max_utility/max_ratio).
        risk_measure: Risk measure (variance/cvar/evar/etc).
        cv_scheme: Optional cross-validation scheme.
        lookback_days: Historical lookback window.
        weights_constraint: Optional {'min': float, 'max': float} weight bounds.
        regime_context: Optional RegimeContext from regime.gate — when provided,
            automatically tightens weight constraints in bear/crisis regimes and
            relaxes them in bull regimes.  The caller's explicit weights_constraint
            takes precedence if both are supplied.
        risk_free_rate: Annualised risk-free rate (fraction, e.g. 0.022). Only
            affects MAXIMIZE_RATIO — that objective's "ratio" is a Sharpe-style
            (return - risk_free) / risk. Callers should resolve this via
            ``app.foundation.settings.get_risk_free_rate(db)`` rather than
            defaulting to 0.0, which overstates every candidate's ratio.
            Converted to the return matrix's own period (see
            ``periods_per_year``) before it reaches skfolio.
        instrument_types: Optional {ticker: instrument_type} map (from
            ``instrument_taxonomy.classify_instrument``). When provided,
            builds skfolio ``groups``/``linear_constraints`` enforcing
            POLICY_WEIGHTS +/- TACTICAL_BAND per asset class (F8/F10/F13) —
            this, not risk_free_rate, is what actually bounds a near-zero-vol
            money-market instrument from dominating the allocation. Additive:
            callers that don't pass this are unaffected.
        previous_weights: Optional {ticker: weight} of the book's current
            weights, for turnover control (paired with max_turnover).
        max_turnover: Optional per-asset cap on |new_weight - previous_weight|.
            Only meaningful together with previous_weights. Silently ignored
            when objective is MAXIMIZE_RATIO ("max_ratio") — its fractional
            reformulation is incompatible with the turnover constraint in
            the installed skfolio/CLARABEL combination (confirmed
            empirically: identical inputs solve under MINIMIZE_RISK, fail
            under MAXIMIZE_RATIO for both VARIANCE and CVAR). A caller on
            that objective still gets the policy-band constraints; it just
            doesn't get turnover control, rather than the whole call failing.
        periods_per_year: Sampling frequency of ``price_matrix`` (ADR 0003:
            pass the named constant, never a bare number). Defaults to
            trading days, matching the daily price matrices every caller
            builds today.

    Returns a dict with weights, expected_return, volatility, risk_metric_value,
    and optionally cv_summary if a cross-validation scheme is selected.
    """
    try:
        import numpy as np
        from skfolio import RiskMeasure
        from skfolio.optimization.convex._base import ObjectiveFunction
        from skfolio.optimization import MeanRisk
        from skfolio.model_selection import (
            WalkForward,
            CombinatorialPurgedCV,
            cross_val_predict,
        )
    except ImportError as exc:
        return {"status": "unavailable", "message": f"skfolio not available: {exc}"}

    try:
        returns = _returns_from_price_matrix(price_matrix)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)}

    if len(returns) < 30:
        return {
            "status": "unavailable",
            "message": f"Need at least 30 return observations; got {len(returns)}.",
        }

    X = returns

    rm_key = _RISK_MEASURE_MAP.get(risk_measure.lower(), "CVAR")
    obj_key = _OBJECTIVE_MAP.get(objective.lower(), "MAXIMIZE_RATIO")
    rm = getattr(RiskMeasure, rm_key)
    obj = getattr(ObjectiveFunction, obj_key)

    # Determine weight bounds: caller's constraint takes precedence;
    # fall back to regime-adjusted constraints, then safe defaults.
    min_weights = 0.0
    max_weights = 1.0
    if regime_context is not None and not weights_constraint:
        # Use regime-aware constraints (tight in bear/crisis, relaxed in bull)
        rc = getattr(regime_context, "constraints", None)
        if rc is None and isinstance(regime_context, dict):
            rc = regime_context.get("constraints")
        if rc is not None:
            min_weights = float(rc.get("min", 0.0))
            max_weights = float(rc.get("max", 1.0))
    if weights_constraint:
        min_weights = float(weights_constraint.get("min", 0.0))
        max_weights = float(weights_constraint.get("max", 1.0))

    groups = None
    linear_constraints = None
    if instrument_types:
        groups, linear_constraints = _build_policy_constraints({
            col: instrument_types.get(col, "equity") for col in returns.columns
        })

    prev_weights_arr = None
    if previous_weights:
        prev_weights_arr = [previous_weights.get(col, 0.0) for col in returns.columns]

    # MAXIMIZE_RATIO's fractional-programming reformulation (Charnes-Cooper)
    # is incompatible with the turnover constraint's per-asset abs-value
    # term in the installed skfolio/CLARABEL combination — confirmed
    # empirically: identical inputs solve fine under MINIMIZE_RISK and fail
    # every time under MAXIMIZE_RATIO, for both VARIANCE and CVAR. Silently
    # dropping max_turnover here (rather than passing it through and letting
    # the whole optimisation fail) keeps the policy-band constraints load
    # bearing even for callers that also pass turnover control.
    effective_max_turnover = max_turnover
    if obj == ObjectiveFunction.MAXIMIZE_RATIO and max_turnover is not None:
        effective_max_turnover = None

    model = MeanRisk(
        objective_function=obj,
        risk_measure=rm,
        min_weights=min_weights,
        max_weights=max_weights,
        risk_free_rate=per_period_risk_free_rate(risk_free_rate, periods_per_year),
        groups=groups,
        linear_constraints=linear_constraints,
        previous_weights=prev_weights_arr,
        max_turnover=effective_max_turnover,
    )

    result: dict[str, Any] = {"status": "unavailable", "objective": objective, "risk_measure": risk_measure}

    try:
        if cv_scheme and cv_scheme.lower() in ("walkforward", "combinatorial"):
            if cv_scheme.lower() == "walkforward":
                cv = WalkForward(test_size=21, train_size=252)
            else:
                cv = CombinatorialPurgedCV()
            pred = cross_val_predict(model, X, cv=cv)
            cv_weights = dict(zip(list(returns.columns), [float(w) for w in np.zeros(len(returns.columns))]))
            try:
                last_ptf = pred
                weights_attr = getattr(last_ptf, "weights", None)
                if weights_attr is not None:
                    cv_weights = {
                        col: float(w)
                        for col, w in zip(list(returns.columns), list(weights_attr))
                    }
            except Exception:
                pass
            sharpe = getattr(pred, "sharpe_ratio", None)
            ann_ret = getattr(pred, "annualized_return", None)
            mdd = getattr(pred, "max_drawdown", None)
            result.update({
                "status": "completed",
                "method": f"cv_{cv_scheme.lower()}",
                "weights": cv_weights,
                "cv_summary": {
                    "sharpe_ratio": float(sharpe) if sharpe is not None else None,
                    "annualized_return": float(ann_ret) if ann_ret is not None else None,
                    "max_drawdown": float(mdd) if mdd is not None else None,
                },
            })
        else:
            model.fit(X)
            weights = {col: float(w) for col, w in zip(list(returns.columns), list(model.weights_))}
            ptf = model.predict(X)
            expected_return = getattr(ptf, "annualized_return", None)
            volatility = getattr(ptf, "annualized_standard_deviation", None)
            sharpe = getattr(ptf, "sharpe_ratio", None)
            result.update({
                "status": "completed",
                "method": "single_period",
                "weights": weights,
                "expected_return": float(expected_return) if expected_return is not None else None,
                "volatility": float(volatility) if volatility is not None else None,
                "sharpe_ratio": float(sharpe) if sharpe is not None else None,
                "risk_metric_value": float(getattr(ptf, rm_key.lower(), 0.0)) if hasattr(ptf, rm_key.lower()) else None,
            })
    except Exception as exc:
        result["status"] = "failed"
        result["message"] = str(exc)

    return result


def run_all_objectives(
    price_matrix: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Run multiple skfolio optimisers at once and return a comparison table."""
    try:
        from skfolio import RiskMeasure
        from skfolio.optimization.convex._base import ObjectiveFunction
        from skfolio.optimization import (
            MeanRisk,
            HierarchicalRiskParity,
            HierarchicalEqualRiskContribution,
            InverseVolatility,
            MaximumDiversification,
        )
    except ImportError as exc:
        return {"status": "unavailable", "message": str(exc)}

    try:
        returns = _returns_from_price_matrix(price_matrix)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)}

    if len(returns) < 30:
        return {"status": "unavailable", "message": f"Need ≥30 observations; got {len(returns)}."}

    X = returns
    cols = list(returns.columns)
    methods: dict[str, Any] = {}

    def _fit_report(name: str, model: Any) -> dict[str, Any]:
        try:
            model.fit(X)
            w = {c: float(v) for c, v in zip(cols, model.weights_)}
            ptf = model.predict(X)
            return {
                "weights": w,
                "expected_return": float(ptf.annualized_return) if hasattr(ptf, "annualized_return") else None,
                "volatility": float(ptf.annualized_standard_deviation) if hasattr(ptf, "annualized_standard_deviation") else None,
                "sharpe": float(ptf.sharpe_ratio) if hasattr(ptf, "sharpe_ratio") else None,
            }
        except Exception as exc:
            return {"status": "failed", "message": str(exc)}

    for rm_name, rm_attr in [("cvar", "CVAR"), ("evar", "EVAR"), ("cdar", "CDAR")]:
        rm = getattr(RiskMeasure, rm_attr)
        methods[f"mean_risk_{rm_name}"] = _fit_report(
            f"mean_risk_{rm_name}",
            MeanRisk(objective_function=ObjectiveFunction.MAXIMIZE_RATIO, risk_measure=rm),
        )

    methods["hrp_cvar"] = _fit_report(
        "hrp_cvar",
        HierarchicalRiskParity(risk_measure=RiskMeasure.CVAR),
    )
    methods["herc"] = _fit_report(
        "herc",
        HierarchicalEqualRiskContribution(risk_measure=RiskMeasure.CVAR),
    )
    methods["inverse_vol"] = _fit_report("inverse_vol", InverseVolatility())
    try:
        methods["max_diversification"] = _fit_report("max_diversification", MaximumDiversification())
    except Exception as exc:
        methods["max_diversification"] = {"status": "failed", "message": str(exc)}

    return {"status": "completed", "methods": methods}


def run_stress_test(
    price_matrix: dict[str, dict[str, float]],
    n_scenarios: int = 500,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Vine-copula stress scenarios via skfolio.

    Aggregates the stressed asset returns using the caller's portfolio
    ``weights`` (falling back to equal weight only when weights are missing or
    don't align to the sampled columns), and reports a genuine CVaR (mean of the
    worst 5% of scenarios) rather than the 5th-percentile VaR.
    """
    try:
        from skfolio.distribution import VineCopula
    except ImportError as exc:
        return {"status": "unavailable", "message": str(exc)}

    try:
        returns = _returns_from_price_matrix(price_matrix)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)}

    if len(returns) < 50:
        return {"status": "unavailable", "message": f"Need ≥50 observations; got {len(returns)}."}

    try:
        from typing import cast

        import numpy as np
        import pandas as pd

        X = returns
        vine = VineCopula()
        vine.fit(X)
        stressed = cast(pd.DataFrame, vine.sample(n_scenarios))

        cols = list(stressed.columns)
        weight_source = "equal"
        w = None
        if weights:
            w = np.array([float(weights.get(c, 0.0)) for c in cols], dtype=float)
            if w.sum() > 0:
                w = w / w.sum()
                weight_source = "portfolio"
            else:
                w = None
        if w is not None:
            ptf_returns = stressed.to_numpy(dtype=float) @ w
        else:
            ptf_returns = np.asarray(stressed.mean(axis=1), dtype=float)

        p05 = float(np.percentile(ptf_returns, 5))
        tail = ptf_returns[ptf_returns <= p05]
        # CVaR = expected loss in the worst 5% of scenarios, as a loss magnitude.
        cvar = -float(tail.mean()) if tail.size else -p05
        return {
            "status": "completed",
            "n_scenarios": n_scenarios,
            "weighting": weight_source,
            "p05_portfolio_return": p05,
            "p50_portfolio_return": float(np.percentile(ptf_returns, 50)),
            "p95_portfolio_return": float(np.percentile(ptf_returns, 95)),
            "mean_portfolio_return": float(ptf_returns.mean()),
            "stressed_cvar_95": max(0.0, cvar),
        }
    except Exception as exc:
        return {"status": "failed", "message": str(exc)}

