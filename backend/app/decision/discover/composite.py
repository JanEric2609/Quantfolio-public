"""Composite scoring for Discovery pipeline.

Combines per-stage signals into a single composite score. The deterministic
quant signals (momentum, risk, benchmark-relative, portfolio fit) always carry
their configured weight. The AlphaCrafter-derived ``ic_icir`` signal (the
key kept for stored configs) is the candidate's IC-weighted exposure to
validated library factors (``stage_alpha_miner``); it is excluded from the
weighting until such factors exist (``ic_signal_usable``) instead of dragging
the composite toward neutral.

There is no ``regime`` signal any more. It was the mean of the screener's
regime-affinity multipliers over five factor categories -- the same number for
every candidate (1.02 bull, 1.04 sideways, 1.02 bear, clipped to 1.0), so it
could not rank anything. Its only effect was to lift candidates with fewer
other signals (funds) through renormalisation (Discover run audit,
2026-09-28).
"""
from __future__ import annotations

import math
from typing import Any

# Ceilings shared with pipeline.py's quality-gate concerns (stage_verification_gate /
# stage_momentum_quality) so the risk signal's penalty scaling matches what the
# gates themselves consider "distressed".
_VOL_MAX = 0.60
_MDD_MAX = 0.30

WEIGHTS: dict[str, float] = {
    "ic_icir": 0.10,
    # "regime" (0.05) was removed on 2026-09-28 without handing its weight
    # to anything: _effective_weights renormalises over the present signals,
    # so every other signal keeps its relative weight. These sum to 0.95.
    "analyst": 0.05,
    "sentiment": 0.05,
    "portfolio": 0.15,
    # 0.05 -> 0.15 (ADR 0017), taking the 0.10 that momentum and benchmark
    # gave up. Value and profitability are among the best-replicated
    # cross-sectional premia (Fama-French 1993/2015; Novy-Marx 2013), and
    # this is the one non-price input. Its 0-1 score moves in a narrow band
    # around 0.5, so in practice the change mostly removes trailing-return
    # dominance rather than handing the ranking to a heuristic.
    # Money-market funds carry no fundamentals, so unlike a higher risk
    # weight this cannot lift the cash attractor (XEON.DE regression test).
    "fundamentals": 0.15,
    # 0.20 -> 0.15 and benchmark 0.10 -> 0.05 (ADR 0017): both measure the
    # same trailing price performance (12-1m return; 3y return vs a
    # benchmark), and the dossier's expected-return anchor is a trailing
    # return too. Together they carried ~35% of effective weight, so one
    # rally was counted two and a half times (BBVA.MC audit, 2026-09-26).
    "momentum": 0.15,
    # Sourced 0.05 from risk's prior 0.15 so WEIGHTS keeps summing to 1.0 --
    # risk is also directly enforced by stage_verification_gate's reject
    # logic, not only this composite weight. Not raised back by ADR 0017: a
    # near-zero-vol money-market fund scores ~1.0 here for structural
    # reasons, so more risk weight re-inflates the cash attractor.
    "risk": 0.10,
    "benchmark": 0.05,
    # Track D1c — advisory, cold-start-gated (dropped from weighting
    # entirely, not defaulted to neutral, until a validated per-ticker
    # model exists).
    "ml_signal": 0.05,
    # Cold-start-gated like ml_signal (dropped, not neutral-defaulted, when
    # no ticker-matched IBES/insider-trading data exists — see
    # has_estimate_data/has_insider_data in derive_signals_from_scores).
    "estimate_revision": 0.05,
    "insider_signal": 0.05,
}

# Fallback momentum squash when no cross-sectional rank exists (a run with
# too few evaluable candidates, or a stored breakdown from before ranks):
# 0.5 + 0.5*tanh(m / scale). Slope 1 at zero like the old linear
# 0.5 + m, but no hard ceiling — the linear version pinned every name up
# 50% or more at exactly 1.0.
MOMENTUM_TANH_SCALE = 0.5

# Minimum IC observations for the AlphaCrafter signal to participate.
# Only breakdowns stored before 2026-09-28 carry n_obs; kept for replays.
THRESHOLD_IC_OBS = 20

# Minimum cross-section the candidate's factor z-scores were computed in
# (pipeline._MIN_EXPOSURE_PANEL). The retired design measured a factor's IC
# across the candidate and three benchmark ETFs: a four-point rank
# correlation, and a property of the factor rather than of the candidate.
MIN_IC_PANEL_SIZE = 30


def ic_signal_usable(ic_data: dict[str, Any] | None, threshold_obs: float = THRESHOLD_IC_OBS) -> bool:
    """Whether the AlphaCrafter exposure is evidence enough to weight in.

    It is when stage_alpha_miner produced an ``exposure_score`` from at least
    one validated factor over a real cross-section. Breakdowns stored before
    the exposure design never qualify (``threshold_obs`` is kept for the
    config-review replay's call signature).
    """
    if not isinstance(ic_data, dict):
        return False
    score = ic_data.get("exposure_score")
    return (
        isinstance(score, (int, float))
        and _num(ic_data.get("n_factors"), 0.0) >= 1
        and _num(ic_data.get("panel_size"), 0.0) >= MIN_IC_PANEL_SIZE
    )

# There is no BUY cutoff on the composite any more: it is a ranking score,
# and a level threshold on it labelled 100% of one week's shortlist BUY and
# 0% of the next week's after a weight change. The verdict comes from the
# cohort's realised track record (candidate_gate.discover_verdict).


def _effective_weights(
    signals: dict[str, float],
    ic_data: dict[str, Any] | None,
    weights: dict[str, float] | None,
) -> dict[str, float]:
    """Renormalised weight per present signal (sums to 1.0, or empty)."""
    w = dict(WEIGHTS)
    if isinstance(weights, dict):
        w.update({k: float(v) for k, v in weights.items()})

    if not ic_signal_usable(ic_data):
        w.pop("ic_icir", None)
    # Stored configs may still weight the retired regime signal.
    w.pop("regime", None)

    sig_dict = signals if isinstance(signals, dict) else {}
    present = {k: wt for k, wt in w.items() if k in sig_dict and wt > 0}
    total = sum(present.values())
    if total <= 0:
        # Degenerate config — fall back to equal weight over present signals.
        keys = [k for k in w if k in sig_dict]
        return {k: 1.0 / len(keys) for k in keys} if keys else {}
    return {k: wt / total for k, wt in present.items()}


def compute_weighted_composite(
    signals: dict[str, float],
    ic_data: dict[str, Any] | None = None,
    weights: dict[str, float] | None = None,
) -> float:
    """Compute a weighted composite score from per-stage signals.

    Weights are merged over :data:`WEIGHTS` so configs stored before a signal
    existed do not silently zero it out, then renormalised over the signals
    actually present. Unless :func:`ic_signal_usable` accepts *ic_data*, the
    advisory ``ic_icir`` signal is dropped from the weighting entirely — the
    deterministic quant signals carry the ranking.

    Args:
        signals: Mapping of signal name → score (0–1).
        ic_data: Optional dict from the alpha-miner stage
            (``exposure_score``, ``n_factors``, ``panel_size``).
        weights: Optional custom weights dict (merged over :data:`WEIGHTS`).

    Returns:
        Float composite score (0–1).
    """
    eff = _effective_weights(signals, ic_data, weights)
    return sum(signals[k] * wt for k, wt in eff.items())


def composite_contributions(
    signals: dict[str, float],
    ic_data: dict[str, Any] | None = None,
    weights: dict[str, float] | None = None,
) -> list[dict[str, float | str]]:
    """Per-signal ``{signal, score, weight, contribution}``, largest first.

    ``weight`` is the renormalised weight actually applied and the
    contributions sum to :func:`compute_weighted_composite`. The dossier used
    to show a hand-picked subset of inputs (IC, fundamentals, analyst — all
    near 0.5) and hide momentum, benchmark and risk, which were what actually
    moved BBVA.MC's score to 0.72.
    """
    eff = _effective_weights(signals, ic_data, weights)
    rows: list[dict[str, float | str]] = [
        {
            "signal": k,
            "score": round(float(signals[k]), 4),
            "weight": round(wt, 4),
            "contribution": round(float(signals[k]) * wt, 4),
        }
        for k, wt in eff.items()
    ]
    rows.sort(key=lambda r: float(r["contribution"]), reverse=True)
    return rows


def _num(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def derive_signals_from_scores(scores: dict[str, Any] | Any) -> dict[str, float]:
    """Map raw per-stage score dicts to the 0-1 signal inputs of :data:`WEIGHTS`.

    This is the single source of truth for turning ``momentum_quality`` /
    ``backtest_vs_benchmark`` / ``verification_gate`` / ``quant_signals`` /
    ``portfolio_fit`` / ``alpha_miner`` /
    ``sentiment_fundamentals`` stage outputs into the named signals that
    :func:`compute_weighted_composite` consumes — used both live in
    ``pipeline.py`` (per-candidate, during a run) and retrospectively in
    ``config_review.py`` (replaying a stored ``signal_breakdown`` to score a
    challenger config). Keeping this in one place is what makes challenger
    evaluation comparable to the production composite it's trying to beat.
    """
    scores_dict = _as_dict(scores)

    alpha_miner = _as_dict(scores_dict.get("alpha_miner"))
    sent_fund = _as_dict(scores_dict.get("sentiment_fundamentals"))
    mom = _as_dict(scores_dict.get("momentum_quality"))
    bt = _as_dict(scores_dict.get("backtest_vs_benchmark"))
    ver = _as_dict(scores_dict.get("verification_gate"))
    quant = _as_dict(scores_dict.get("quant_signals"))
    fit = _as_dict(scores_dict.get("portfolio_fit"))
    ml = _as_dict(scores_dict.get("ml_signal"))

    # The candidate's IC-weighted exposure to validated AlphaCrafter factors
    # (stage_alpha_miner). Breakdowns stored before it carry only the retired
    # panel IC/ICIR; ic_signal_usable keeps those out of the weighting.
    exposure = alpha_miner.get("exposure_score")
    if isinstance(exposure, (int, float)):
        ic_score = max(0.0, min(1.0, float(exposure)))
    else:
        ic = _num(alpha_miner.get("ic"), 0.0)
        icir = _num(alpha_miner.get("icir"), 0.0)
        ic_score = max(0.0, min(1.0, 0.5 + ic * 3.0 + icir * 0.25))

    # Fundamentals/analyst-consensus scores are structurally inapplicable
    # (not just temporarily missing) when every underlying accounting metric
    # is null — e.g. money-market/cash-equivalent ETFs, which have no P/E,
    # book value, or analyst coverage at all. Rather than defaulting those
    # signals to a neutral 0.5 and still weighting them into the composite
    # as if they were real-but-uninformative evidence, drop the keys
    # entirely so compute_weighted_composite renormalises over what remains
    # — the same treatment already given to ic_icir when the IC evidence is
    # too thin.
    factor_characteristics_score = quant.get("factor_characteristics_score")
    has_fundamentals_data = (
        any(
            sent_fund.get(k) is not None
            for k in ("pe", "pb", "roe", "de_ratio", "market_cap", "revenue_growth")
        )
        or factor_characteristics_score is not None
    )
    has_sentiment_data = _num(sent_fund.get("social_mentions"), 0.0) > 0

    fit_score = _num(fit.get("fit_score"), 0.5)

    # Wiring fix (F9, #4): pipeline.py's stage_quant_signals stamps
    # scores["quant_signals"]["instrument_type"] = "money_market" specifically
    # so this function can neutralise the momentum signal for cash
    # equivalents — a near-zero-vol money-market fund's 12-1m momentum
    # doesn't carry the same evidence an equity's does, it just reflects the
    # current policy rate drifting. This module previously never read the
    # field at all, despite a comment elsewhere in the pipeline claiming it
    # did. Forced to a flat neutral 0.5 (rather than dropped from the
    # weighting) deliberately: dropping the key renormalises the remaining
    # weight onto "risk", which already scores near 1.0 for a near-zero-vol
    # instrument for structural reasons — renormalising would inflate
    # exactly the cash-attractor effect this fix is meant to reduce.
    instrument_type = quant.get("instrument_type", "equity")

    # Cross-sectional percentile rank of 12-1m momentum within the run
    # (pipeline.assign_momentum_ranks), stamped into the stored scores so a
    # replay here reproduces it. Ranking is how momentum is conventionally
    # scored (Jegadeesh & Titman 1993; Asness, Moskowitz & Pedersen 2013):
    # what predicts is beating the other candidates, not clearing a fixed
    # return level that a broad rally lifts everyone over.
    momentum_raw = _num(mom.get("momentum_12_1m"), 0.0)
    momentum_rank = mom.get("momentum_rank")
    if instrument_type == "money_market":
        momentum_score = 0.5
    elif isinstance(momentum_rank, (int, float)):
        momentum_score = max(0.0, min(1.0, float(momentum_rank)))
    else:
        momentum_score = 0.5 + 0.5 * math.tanh(momentum_raw / MOMENTUM_TANH_SCALE)

    vol_raw = _num(ver.get("volatility"), _num(mom.get("volatility_6m"), 0.0))
    cvar_raw = abs(_num(quant.get("cvar_95_daily"), 0.0))
    mdd_raw = abs(_num(ver.get("max_drawdown"), 0.0))
    vol_pen = min(1.0, vol_raw / _VOL_MAX) if _VOL_MAX else 0.0
    cvar_pen = min(1.0, cvar_raw / 0.06)
    mdd_pen = min(1.0, mdd_raw / _MDD_MAX) if _MDD_MAX else 0.0
    risk_score = max(0.0, min(1.0, 1.0 - (0.4 * vol_pen + 0.3 * cvar_pen + 0.3 * mdd_pen)))
    if instrument_type == "money_market":
        # Same reasoning as momentum above: a cash fund's ~1.0 risk score is
        # structural (no volatility by construction), not evidence of a good
        # pick. Once momentum became a rank (ADR 0017) and equity composites
        # stopped saturating, it alone lifted XEON.DE into a replayed top 15.
        risk_score = 0.5

    excess = bt.get("excess_return_annual")
    sharpe_delta = bt.get("sharpe_delta")
    active_t = bt.get("active_t_stat")
    if isinstance(active_t, (int, float)):
        # Risk-adjusted: the t-statistic of the 3y active return (information
        # ratio x sqrt(years)), squashed so t=±2 maps to ~0.88/0.12. Raw
        # excess return let a volatile stock's rally saturate this signal
        # (BBVA.MC: +45%/yr excess -> 0.97) regardless of how much noise
        # came with it.
        benchmark_score = 0.5 + 0.5 * math.tanh(float(active_t) / 2.0)
    elif isinstance(excess, (int, float)):
        # Legacy formula, kept so replays of breakdowns stored before
        # active_t_stat existed (config_review) score as they did live.
        # sharpe_delta = candidate_sharpe - benchmark_sharpe, and Sharpe
        # explodes toward +/-infinity as volatility -> 0 — it is not
        # evidence of genuine risk-adjusted outperformance for near-zero-vol
        # candidates (money-market/cash ETFs), only an artifact of dividing
        # by a tiny denominator. Prod incident: XEON.DE's ~0.2% volatility
        # produced candidate_sharpe=12.29 (benchmark 1.15, sharpe_delta=11.14),
        # which alone saturated benchmark_score to 1.0 despite
        # excess_return_annual=-15.6%/yr (the candidate's own "trails
        # benchmark" concern). Bound sharpe_delta to a small nudge so it can
        # tilt but never override what excess_return_annual already shows.
        sharpe_nudge = max(-0.1, min(0.1, 0.02 * _num(sharpe_delta, 0.0)))
        benchmark_score = 0.5 + float(excess) + sharpe_nudge
        benchmark_score = max(0.0, min(1.0, benchmark_score))
    else:
        benchmark_score = 0.5

    signals: dict[str, float] = {
        "ic_icir": ic_score,
        "portfolio": fit_score,
        "momentum": momentum_score,
        "risk": risk_score,
        "benchmark": benchmark_score,
    }
    if has_fundamentals_data:
        # Blend the live-provider fundamentals_score with the gvkey-verified
        # WRDS/JKP factor-characteristics score (stage_quant_signals) when
        # both exist; fall back to whichever one is actually present rather
        # than defaulting the missing side to neutral 0.5 and diluting the
        # one real reading.
        provider_score = sent_fund.get("fundamentals_score")
        if provider_score is not None and factor_characteristics_score is not None:
            signals["fundamentals"] = max(
                0.0,
                min(1.0, 0.5 * _num(provider_score, 0.5) + 0.5 * _num(factor_characteristics_score, 0.5)),
            )
        elif factor_characteristics_score is not None:
            signals["fundamentals"] = _num(factor_characteristics_score, 0.5)
        else:
            signals["fundamentals"] = _num(provider_score, 0.5)
    # Only real consensus data participates: stage_sentiment_fundamentals
    # leaves the score None (with an analyst_unavailable concern) when no
    # provider answered, instead of a neutral 0.5 weighted in as evidence.
    # Breakdowns stored before that change carry 0.5 and replay unchanged.
    # Breakdowns stored before analyst_source existed only trusted the score
    # alongside real fundamentals (a fund's all-null payload still carried
    # the 0.5 placeholder), so they keep that gate.
    # With enough coverage in the run, the within-sector rank of target
    # upside (pipeline.assign_analyst_ranks) replaces the absolute score.
    analyst_score = sent_fund.get("analyst_estimate_score")
    analyst_rank = sent_fund.get("analyst_rank")
    if isinstance(analyst_score, (int, float)) and (
        sent_fund.get("analyst_source") is not None or has_fundamentals_data
    ):
        chosen = analyst_rank if isinstance(analyst_rank, (int, float)) else analyst_score
        signals["analyst"] = max(0.0, min(1.0, float(chosen)))
    if has_sentiment_data:
        signals["sentiment"] = _num(sent_fund.get("sentiment_score"), 0.5)
    # ml_signal: dropped entirely (not defaulted to neutral 0.5) when no
    # validated model exists -- "no ML opinion" is not neutral evidence,
    # same treatment as fundamentals/sentiment above. The pooled model's
    # prediction is already a 0-1 percentile of the universe; stored
    # per-ticker classifier outputs (-1/0/+1, before 2026-09-28) keep
    # their old mapping for replays.
    ml_prediction = ml.get("prediction")
    if ml_prediction is not None:
        if ml.get("signal_kind") == "ml_pooled":
            signals["ml_signal"] = max(0.0, min(1.0, _num(ml_prediction, 0.5)))
        else:
            signals["ml_signal"] = max(0.0, min(1.0, 0.5 + 0.25 * _num(ml_prediction, 0.0)))

    # estimate_revision / insider_signal (ticker-matched IBES / SEC data):
    # dropped entirely, not neutral-defaulted, when the best-effort ticker
    # join found nothing — same "no opinion is not neutral evidence"
    # treatment as fundamentals/sentiment/ml_signal above.
    est = _as_dict(scores_dict.get("estimate_revision_signal"))
    has_estimate_data = est.get("sue") is not None or est.get("revision_momentum") is not None
    if has_estimate_data:
        sue = _num(est.get("sue"), 0.0)
        revision_momentum = _num(est.get("revision_momentum"), 0.0)
        # SUE and revision momentum are unbounded standardized/fractional
        # values; squash them onto 0-1 around a neutral 0.5 the same way
        # momentum_score/benchmark_score do above.
        signals["estimate_revision"] = max(
            0.0, min(1.0, 0.5 + 0.15 * sue + 0.5 * revision_momentum)
        )

    insider = _as_dict(scores_dict.get("insider_signal"))
    has_insider_data = insider.get("cluster_buy_score") is not None
    if has_insider_data:
        cluster = _num(insider.get("cluster_buy_score"), 0.0)
        flow = _num(insider.get("net_insider_flow_usd"), 0.0)
        # Cluster buying is the stronger, more robust signal (Cohen/Malloy/
        # Pomorski) so it dominates; net dollar flow nudges within a capped
        # band so one large sale can't swing the score as hard as a
        # multi-insider cluster buy does.
        flow_nudge = max(-0.15, min(0.15, flow / 1_000_000.0))
        signals["insider_signal"] = max(0.0, min(1.0, 0.5 + 0.1 * cluster + flow_nudge))

    return signals
