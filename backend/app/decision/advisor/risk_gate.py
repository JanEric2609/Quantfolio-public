"""Hard risk gate for the advisor loop (C2).

Rejects any proposed trade/portfolio state that pushes VaR, CVaR, or max
drawdown past configured ceilings. The gate is deterministic and runs **once**,
before the LLM sees candidates. Nothing re-checks the envelope after the model
answers, so ``llm_decision`` is what keeps a gate-blocked holding sell-only —
see ``sell_only_symbols`` there.

Default ceilings (config-overridable via the ``advisor`` public settings or a
``ceilings`` argument):

- ``var_95_daily``:  3% — daily 95% historical VaR of the proposed book.
- ``cvar_95_daily``: 4.5% — daily 95% CVaR (expected tail loss).
- ``max_drawdown``:  25% — historical max drawdown of the proposed book.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.decision.advisor.decision import TradeProposal, _portfolio_risk_envelope
from app.foundation.quant_optim import POLICY_GROUP, POLICY_WEIGHTS, TACTICAL_BAND

logger = logging.getLogger(__name__)

DEFAULT_RISK_CEILINGS: dict[str, float] = {
    "var_95_daily": 0.03,
    "cvar_95_daily": 0.045,
    "max_drawdown": 0.25,
}

# F10: dropping names by worst MC P5 tail (below) always removes the
# riskiest — i.e. most growth-shaped — names first, since a money-market
# name's P5 is closest to zero. Unchecked, a breach on a handful of volatile
# growth names can whittle the book down to an all-cash-like remainder,
# reproducing the exact cash-concentration failure Workstream 4 fixes at the
# optimiser level. This floor keeps remediation from doing the same thing
# through a different path: growth (equity+etf+bond) weight must not drop
# below the bottom of its policy band.
GROWTH_FLOOR = POLICY_WEIGHTS["equity"] + POLICY_WEIGHTS["bond"] - TACTICAL_BAND


@dataclass
class RiskGateResult:
    """Outcome of a risk-gate check."""

    passed: bool
    envelope: dict[str, float | None]
    ceilings: dict[str, float]
    breaches: list[str] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    passing_symbols: list[str] = field(default_factory=list)


def _envelope_breaches(
    envelope: dict[str, float | None],
    ceilings: dict[str, float],
) -> list[str]:
    breaches: list[str] = []
    for metric, ceiling in ceilings.items():
        value = envelope.get(metric)
        if isinstance(value, (int, float)) and value > ceiling:
            breaches.append(f"{metric} {value:.4f} exceeds ceiling {ceiling:.4f}")
    return breaches


def check_risk_gate(
    proposal: TradeProposal,
    current_risk: dict[str, float | None] | None = None,
    *,
    ceilings: dict[str, float] | None = None,
    series_by_symbol: dict[str, Any] | None = None,
    instrument_types: dict[str, str] | None = None,
) -> RiskGateResult:
    """Check a trade proposal against hard risk ceilings.

    If the proposed portfolio envelope breaches a ceiling, the riskiest names
    (by MC P5 tail) are dropped one at a time — each recorded in ``blocked``
    with a reason — until the remaining book passes or nothing is left.

    Args:
        proposal: Deterministic proposal from ``build_trade_proposal``.
        current_risk: Optional current-book envelope (informational; a breach
            already present in the current book is logged, not double-counted).
        ceilings: Overrides for :data:`DEFAULT_RISK_CEILINGS`.
        series_by_symbol: Optional price-series map for envelope recomputation
            after dropping names. When absent, the gate can only pass/fail the
            proposal as a whole.
        instrument_types: Optional {ticker: instrument_type} map. When
            provided, remediation respects GROWTH_FLOOR (F10) — it will not
            drop a growth-classified (equity/etf/bond) name once doing so
            would push total growth weight below the floor, preferring to
            drop a cash-classified name instead. Without this, dropping
            worst-tail-risk names always removes growth names first (a
            money-market name's P5 is closest to zero), which can whittle
            the book down to all-cash — the same failure mode Workstream 4
            fixes at the optimiser level, reproduced through remediation.

    Returns:
        RiskGateResult with the passing subset and per-name block reasons.
    """
    limits = {**DEFAULT_RISK_CEILINGS, **(ceilings or {})}
    weights = dict(proposal.suggested_weights)
    blocked: list[dict[str, str]] = []

    if current_risk:
        pre = _envelope_breaches(current_risk, limits)
        if pre:
            logger.warning("risk gate: current book already breaches: %s", "; ".join(pre))

    envelope = dict(proposal.risk_envelope)
    breaches = _envelope_breaches(envelope, limits)

    def _is_growth(sym: str) -> bool:
        itype = (instrument_types or {}).get(sym, "equity")
        return POLICY_GROUP.get(itype, "equity") != "cash"

    floor_impasse = False

    if breaches and series_by_symbol:
        # Drop the riskiest names (worst MC P5 tail, fallback: largest weight)
        # until the envelope passes — skipping a growth name that would
        # breach GROWTH_FLOOR in favor of the next-worst non-growth name.
        def _tail_risk(sym: str) -> float:
            summary = proposal.mc_summaries.get(sym)
            return summary.p5 if summary is not None else 0.0

        original_total = sum(weights.values()) or 1.0

        while breaches and len(weights) > 1:
            growth_weight = sum(w for s, w in weights.items() if _is_growth(s))
            ranked = sorted(weights, key=lambda s: (_tail_risk(s), -weights[s]))
            worst: str | None = None
            for candidate in ranked:
                if (
                    instrument_types is not None
                    and _is_growth(candidate)
                    and growth_weight - weights[candidate] < GROWTH_FLOOR * original_total
                ):
                    continue
                worst = candidate
                break
            if worst is None:
                # Every remaining name is protected by the growth floor —
                # cannot fix the breach further without violating policy.
                # Unlike genuine exhaustion (down to 1 name), the remaining
                # names are intentionally protected, not stuck — leave them
                # passing rather than falling through to "block everything",
                # which would defeat the floor's whole purpose.
                floor_impasse = True
                break
            weights.pop(worst)
            blocked.append({
                "symbol": worst,
                "reason": f"dropped to satisfy risk ceilings ({'; '.join(breaches)})",
            })
            envelope = _portfolio_risk_envelope(weights, series_by_symbol)
            breaches = _envelope_breaches(envelope, limits)

        if floor_impasse and breaches:
            logger.warning(
                "risk gate: could not fully resolve breach (%s) without dropping "
                "growth weight below GROWTH_FLOOR (%.2f) — leaving remaining "
                "names passing with the residual breach reported",
                "; ".join(breaches), GROWTH_FLOOR,
            )

    if breaches and not floor_impasse:
        # Could not fix by dropping names — block everything.
        for sym in list(weights):
            blocked.append({"symbol": sym, "reason": "; ".join(breaches)})
        weights = {}

    return RiskGateResult(
        passed=not breaches,
        envelope=envelope,
        ceilings=limits,
        breaches=breaches,
        blocked=blocked,
        passing_symbols=sorted(weights.keys()),
    )
