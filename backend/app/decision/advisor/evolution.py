"""Champion vs challenger evolution engine (PR2 C3).

Each round: both sleeves run their daily advisor cycle (same universe, same
real-book seed basis, same cadence), get scored on the 4-axis scorecard, and
the round is recorded as ``CompetitionRun``/``CompetitionDecision`` rows so
graduation's ``_competition_win_rate`` keeps working.

Promotion is guarded against small-sample luck: the challenger must beat the
champion's composite 4-axis score by ``PROMOTION_MARGIN`` with at least
``MIN_RESOLVED_DECISIONS`` resolved predictions on BOTH sleeves. A marginal
winner keeps competing; a decisive loser is retired and a fresh mutation
spawned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import UTC
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    AdvisorScorecard,
    AdvisorStrategy,
    CompetitionDecision,
    CompetitionRun,
    PaperPortfolio,
)
from app.foundation.models.entities._core import now_utc
from app.decision.advisor.cycle import run_advisor_cycle
from app.decision.advisor.reflection import reflect_on_cycle
from app.decision.advisor.scorecard import compute_advisor_scorecard
from app.decision.advisor.strategy import (
    ensure_champion,
    get_active_challenger,
    promote_challenger,
    spawn_challenger,
)
from app.foundation.models.entities import DiscoveryPrediction

logger = logging.getLogger(__name__)

MIN_RESOLVED_DECISIONS = 20
PROMOTION_MARGIN = 0.02
_COINFLIP_BRIER = 0.25
_MDD_CEILING = 0.25

EVOLUTION_RUN_NAME = "Champion vs Challenger"


def composite_score(scorecard: AdvisorScorecard | None) -> tuple[float | None, dict[str, Any]]:
    """Collapse the 4 axes into one comparable 0-1 composite.

    Axes that are NULL are excluded from the average — except ``magnitude``
    (F15): the composite is ``None`` unless ``magnitude`` specifically is
    computable, not just "any 3 of 4" axes. A forecaster with no magnitude
    signal at all is exactly the case this axis exists to catch — silently
    averaging over risk_adjusted/calibration/downside without it previously
    scored an all-cash book 0.94/1.0, since a flat, low-vol book trivially
    aces the other three axes with nothing to show it never forecasts a
    magnitude. Honest "insufficient data" (``None``), never a placeholder.
    Components:

    - risk-adjusted return: Probabilistic Sharpe Ratio (F11) — already a
      probability in [0,1] that the true Sharpe beats cash's own (zero-
      excess) Sharpe, accounting for sample size/skew/kurtosis. Falls back
      to sigmoid(sharpe) only for scorecard rows computed before PSR existed
      (``scorecard.psr is None`` but ``scorecard.sharpe`` is set).
    - calibration: 1 − bucketed RPS (F11), floored at 0. RPS checks whether
      the predicted p5/p95 magnitude band actually captured the realised
      outcome as often as it should — unlike Brier, which only ever checked
      direction. Falls back to ``1 − brier/0.25`` (0.25 = coin-flip Brier)
      only for scorecard rows computed before rps_avg existed.
    - magnitude: R² × closeness of Mincer-Zarnowitz slope to 1.
    - downside: 1 − max_drawdown/0.25, floored at 0.
    """
    if scorecard is None:
        return None, {"note": "no scorecard"}
    parts: dict[str, float] = {}
    if scorecard.psr is not None:
        parts["risk_adjusted"] = max(0.0, min(1.0, float(scorecard.psr)))
    elif scorecard.sharpe is not None:
        # Transitional fallback for scorecard rows computed before PSR
        # existed. Annualised Sharpe can be extreme on short windows —
        # clamp before the sigmoid so it saturates instead of overflowing.
        sharpe = max(-20.0, min(20.0, float(scorecard.sharpe)))
        parts["risk_adjusted"] = 1.0 / (1.0 + math.exp(-sharpe))
    if scorecard.rps_avg is not None:
        parts["calibration"] = max(0.0, 1.0 - float(scorecard.rps_avg))
    elif scorecard.brier_avg is not None:
        # Transitional fallback for scorecard rows computed before rps_avg
        # existed.
        parts["calibration"] = max(0.0, 1.0 - float(scorecard.brier_avg) / _COINFLIP_BRIER)
    if scorecard.mz_r2 is not None and scorecard.mz_slope is not None:
        closeness = max(0.0, 1.0 - abs(float(scorecard.mz_slope) - 1.0))
        parts["magnitude"] = max(0.0, float(scorecard.mz_r2)) * closeness
    if scorecard.max_drawdown is not None:
        parts["downside"] = max(0.0, 1.0 - float(scorecard.max_drawdown) / _MDD_CEILING)

    detail: dict[str, Any] = {"components": {k: round(v, 4) for k, v in parts.items()}}
    if "magnitude" not in parts:
        detail["note"] = (
            f"only {len(parts)}/4 axes computable and magnitude is not among them — "
            "composite withheld (F15: magnitude is required, not just any 3 of 4)"
        )
        return None, detail
    if len(parts) < 3:
        detail["note"] = f"only {len(parts)}/4 axes computable — composite withheld"
        return None, detail
    value = sum(parts.values()) / len(parts)
    detail["composite"] = round(value, 4)
    return value, detail


def get_scorecard_history(
    db: Session, user_id: str, *, portfolio_id: str | None = None
) -> dict[str, Any] | None:
    """Read-only: the most recently *persisted* AdvisorScorecard for a sleeve,
    collapsed to a plain dict plus its composite score.

    Does not compute or persist a fresh scorecard — that's
    ``scorecard.compute_advisor_scorecard``'s job (it upserts, which is the
    wrong side effect for a read-only consumer like recommendation_engine).
    Defaults to the user's most recently scored sleeve when *portfolio_id* is
    omitted. Returns ``None`` if no scorecard has ever been computed yet.
    """
    query = db.query(AdvisorScorecard).filter(AdvisorScorecard.user_id == user_id)
    if portfolio_id is not None:
        query = query.filter(AdvisorScorecard.portfolio_id == portfolio_id)
    row = query.order_by(AdvisorScorecard.window_end.desc()).first()
    if row is None:
        return None
    score, _detail = composite_score(row)
    return {
        "portfolio_id": row.portfolio_id,
        "window_end": row.window_end.isoformat(),
        "n_predictions": row.n_predictions,
        "n_resolved": row.n_resolved,
        "sharpe": row.sharpe,
        "sortino": row.sortino,
        "calmar": row.calmar,
        "psr": row.psr,
        "brier_avg": row.brier_avg,
        "rps_avg": row.rps_avg,
        "mz_slope": row.mz_slope,
        "mz_r2": row.mz_r2,
        "max_drawdown": row.max_drawdown,
        "composite_score": score,
    }


def next_resolution_at(db: Session, portfolio_id: str) -> str | None:
    """Earliest ``resolve_at`` among *portfolio_id*'s still-pending predictions.

    The composite score is withheld until enough axes are computable, which
    needs *resolved* predictions — and predictions take the full horizon to
    resolve. The UI uses this to show "next evaluation: ~{date}" instead of a
    bare "insufficient data" while the horizon is simply still elapsing.
    Returns ``None`` when there are no pending predictions left to resolve
    (e.g. a brand-new sleeve with no predictions at all).
    """
    rows = (
        db.query(DiscoveryPrediction.resolve_at)
        .filter(
            DiscoveryPrediction.portfolio_id == portfolio_id,
            DiscoveryPrediction.outcome_status == "pending",
        )
        .all()
    )
    ref = now_utc()
    future = [
        resolve_at if resolve_at.tzinfo is not None else resolve_at.replace(tzinfo=UTC)
        for (resolve_at,) in rows
        if resolve_at is not None
    ]
    future = [stamp for stamp in future if stamp > ref]
    if not future:
        return None
    return min(future).isoformat()


def _recent_decisions_payload(db: Session, user_id: str, portfolio_id: str) -> list[dict[str, Any]]:
    """Resolved predictions for a sleeve, shaped for reflection."""
    rows = (
        db.query(DiscoveryPrediction)
        .filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.portfolio_id == portfolio_id,
            DiscoveryPrediction.outcome_status == "resolved",
        )
        .order_by(DiscoveryPrediction.predicted_at.desc())
        .limit(50)
        .all()
    )
    from app.foundation.models.entities import DiscoverCandidate
    from app.foundation.instrument_taxonomy import classify_instrument

    # The candidate's own name (e.g. "Xtrackers II EUR Overnight Rate Swap
    # UCITS ETF") is what money-market detection keys off — DiscoveryPrediction
    # itself doesn't store it, but the DiscoverCandidate row from the same
    # run does. Batched by (run_id, symbol) rather than a live per-symbol
    # lookup so this stays a single extra query regardless of decision count.
    run_symbol_pairs = {(r.run_id, r.symbol) for r in rows}
    names_by_run_symbol: dict[tuple[str, str], str | None] = {}
    if run_symbol_pairs:
        run_ids = {run_id for run_id, _ in run_symbol_pairs}
        cand_rows = (
            db.query(DiscoverCandidate.run_id, DiscoverCandidate.symbol, DiscoverCandidate.name)
            .filter(DiscoverCandidate.run_id.in_(run_ids))
            .all()
        )
        names_by_run_symbol = {(run_id, symbol): name for run_id, symbol, name in cand_rows}

    return [
        {
            "ticker": r.symbol,
            # Grounds the reflection LLM's "sectors" tag in an actual
            # classification instead of leaving it to invent one — without
            # this, a candidate with no sector data at all (e.g. a
            # money-market ETF) gets a fabricated sector label (prod
            # incident: XEON.DE tagged "technology" in a persisted
            # strategy_lessons row, replayed into future decision prompts).
            "instrument_type": classify_instrument(
                r.symbol, name=names_by_run_symbol.get((r.run_id, r.symbol))
            ),
            "direction": r.direction,
            "confidence_raw": r.conviction,
            "confidence_calibrated": r.conviction_calibrated,
            "expected_return": r.expected_return,
            "realised_return": r.realised_return,
            "thesis": r.thesis,
        }
        for r in rows
    ]


def _open_evolution_run(
    db: Session, champion: AdvisorStrategy, challenger: AdvisorStrategy
) -> CompetitionRun:
    """Find or create the CompetitionRun for this champion/challenger pairing."""
    run = (
        db.query(CompetitionRun)
        .filter(
            CompetitionRun.portfolio_a_id == champion.portfolio_id,
            CompetitionRun.portfolio_b_id == challenger.portfolio_id,
            CompetitionRun.status.in_(["init", "observe"]),
        )
        .order_by(CompetitionRun.created_at.desc())
        .first()
    )
    if run is not None:
        return run
    run = CompetitionRun(
        portfolio_a_id=champion.portfolio_id,
        portfolio_b_id=challenger.portfolio_id,
        name=EVOLUTION_RUN_NAME,
        cadence_days=1,
        status="observe",
        state_json=json.dumps(
            {
                "kind": "evolution",
                "champion_strategy_id": champion.id,
                "challenger_strategy_id": challenger.id,
            }
        ),
    )
    db.add(run)
    db.flush()
    return run


def _record_round(
    db: Session,
    run: CompetitionRun,
    *,
    portfolio_id: str,
    score: float | None,
    detail: dict[str, Any],
    scorecard: AdvisorScorecard | None,
    winner: bool,
    council_result_json: str | None = None,
    debate_report_json: str | None = None,
) -> CompetitionDecision:
    row = CompetitionDecision(
        run_id=run.id,
        round_number=run.current_round,
        portfolio_id=portfolio_id,
        score_json=json.dumps(
            {
                "composite": score,
                **detail,
                "n_resolved": scorecard.n_resolved if scorecard else 0,
            },
            default=str,
        ),
        winner=winner,
    )
    if council_result_json is not None:
        row.council_result_json = council_result_json
    if debate_report_json is not None:
        row.debate_report_json = debate_report_json
    db.add(row)
    return row


def _strategy_prompt(strategy: AdvisorStrategy) -> str:
    """Minimal LLM-facing framing for a sleeve — role + its mutated config.

    Not a persisted or user-facing artifact; only feeds the council's
    ``strategy`` context key, whose agents already tolerate plain text.
    """
    return (
        f"Advisor {strategy.role} strategy (id={strategy.id[:8]}). "
        f"Config: {json.dumps(strategy.config_json or {}, default=str)}"
    )


def _run_competition_council(
    db: Session,
    *,
    champion: AdvisorStrategy,
    challenger: AdvisorStrategy,
    run_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Run the real Multi-Agent Council competition for one evolution round.

    Returns ``(champion_council_result_json, challenger_council_result_json,
    debate_report_json)`` — each a serialised ``CouncilResult``/``DebateReport``
    JSON string, or all ``None`` on any failure. This never blocks or alters
    promotion: scoring
    and promotion are computed entirely from ``AdvisorScorecard`` (real paper
    P&L), not from the council. A failure here only means
    ``CompetitionDecision.council_result_json`` stays at its default for this
    round, exactly as it always has.

    CRITICAL: the champion's row must get ``result.portfolio_a`` and the
    challenger's row must get ``result.portfolio_b`` — matching how
    ``context_a``/``context_b`` are built below. ``/api/llm-portfolio``'s
    journal reads ``council_result_json`` as ONE portfolio's own
    ``CouncilResult`` ({portfolio_id, analysis, risk, macro, decision,
    errors}), not the wrapping ``CompetitionCouncilResult`` — swapping this
    pairing or storing the wrapper reproduces the exact "always shows hold"
    bug this wiring exists to fix.
    """
    from app.decision import llm_portfolio
    from app.foundation.settings import resolve_llm_base_url, resolve_llm_model

    if champion.portfolio_id is None or challenger.portfolio_id is None:
        return None, None, None
    try:
        champ_portfolio = db.query(PaperPortfolio).filter(
            PaperPortfolio.id == champion.portfolio_id
        ).one()
        chall_portfolio = db.query(PaperPortfolio).filter(
            PaperPortfolio.id == challenger.portfolio_id
        ).one()

        context_a = llm_portfolio.build_council_context(
            db, champ_portfolio, strategy_prompt=_strategy_prompt(champion),
        )
        context_b = llm_portfolio.build_council_context(
            db, chall_portfolio, strategy_prompt=_strategy_prompt(challenger),
        )

        base_url = resolve_llm_base_url(db)
        model = resolve_llm_model(db)
        config_a = llm_portfolio.create_default_config(
            champ_portfolio.id, _strategy_prompt(champion), base_url, model,
        )
        config_b = llm_portfolio.create_default_config(
            chall_portfolio.id, _strategy_prompt(challenger), base_url, model,
        )
        debate_config = llm_portfolio.create_default_config(
            "debate", "Adjudicate the champion vs. challenger evolution round.", base_url, model,
        )
        orchestrator = llm_portfolio.CompetitionOrchestrator(config_a, config_b, debate_config)

        competition_rules = (
            f"Champion vs challenger evolution round. Challenger promotes if its "
            f"composite score beats the champion's by more than {PROMOTION_MARGIN}, "
            f"with at least {MIN_RESOLVED_DECISIONS} resolved predictions on both sleeves."
        )
        result = asyncio.run(
            orchestrator.run_competition(context_a, context_b, competition_rules, run_id)
        )
        champ_json = json.dumps(result.portfolio_a.model_dump(mode="json"))
        chall_json = json.dumps(result.portfolio_b.model_dump(mode="json"))
        debate_json = json.dumps(result.debate.model_dump(mode="json"))
        return champ_json, chall_json, debate_json
    except Exception as exc:
        logger.warning("Competition council failed for run %s: %s", run_id, exc)
        return None, None, None


def run_evolution_round(
    db: Session,
    user_id: str,
    *,
    llm_call: Callable[[Session, list[dict[str, str]]], str] | None = None,
    min_resolved: int = MIN_RESOLVED_DECISIONS,
    margin: float = PROMOTION_MARGIN,
    reflect: bool = True,
) -> dict[str, Any]:
    """Run one champion-vs-challenger evolution round for *user_id* (C3).

    Steps: ensure both strategies exist → run both sleeves' daily cycles →
    score both on the 4-axis card → record the round → promote / retire /
    keep-observing under the small-sample guard → reflect lessons.

    Returns a summary dict with scores, the verdict, and any strategy churn.
    """
    champion = ensure_champion(db, user_id)
    challenger = get_active_challenger(db, user_id) or spawn_challenger(db, user_id)
    if champion.portfolio_id is None or challenger.portfolio_id is None:
        raise ValueError("champion/challenger strategy is missing its backing portfolio_id")

    champ_cycle = run_advisor_cycle(db, user_id, strategy=champion, llm_call=llm_call)
    chall_cycle = run_advisor_cycle(db, user_id, strategy=challenger, llm_call=llm_call)

    champ_card = compute_advisor_scorecard(db, user_id, portfolio_id=champion.portfolio_id)
    chall_card = compute_advisor_scorecard(db, user_id, portfolio_id=challenger.portfolio_id)

    champ_score, champ_detail = composite_score(champ_card)
    chall_score, chall_detail = composite_score(chall_card)

    n_champ = champ_card.n_resolved if champ_card else 0
    n_chall = chall_card.n_resolved if chall_card else 0
    sample_ok = n_champ >= min_resolved and n_chall >= min_resolved
    scores_ok = champ_score is not None and chall_score is not None

    verdict = "observing"
    if scores_ok and sample_ok:
        assert champ_score is not None and chall_score is not None
        if chall_score > champ_score + margin:
            verdict = "promote"
        elif chall_score < champ_score - margin:
            verdict = "retire_challenger"
        else:
            verdict = "marginal_keep_observing"
    elif scores_ok and not sample_ok:
        verdict = "insufficient_sample"

    run = _open_evolution_run(db, champion, challenger)
    champ_council_json, chall_council_json, debate_json = _run_competition_council(
        db, champion=champion, challenger=challenger, run_id=run.id,
    )
    challenger_wins = verdict == "promote"
    _record_round(
        db, run,
        portfolio_id=champion.portfolio_id,
        score=champ_score, detail=champ_detail, scorecard=champ_card,
        winner=scores_ok and sample_ok and not challenger_wins,
        council_result_json=champ_council_json,
        debate_report_json=debate_json,
    )
    _record_round(
        db, run,
        portfolio_id=challenger.portfolio_id,
        score=chall_score, detail=chall_detail, scorecard=chall_card,
        winner=challenger_wins,
        council_result_json=chall_council_json,
        debate_report_json=debate_json,
    )
    run.current_round += 1
    run.updated_at = now_utc()

    promoted_id: str | None = None
    new_challenger_id: str | None = None
    if verdict == "promote":
        promote_challenger(db, challenger)
        promoted_id = challenger.id
        run.status = "completed"
        run.ended_at = now_utc()
        new_challenger_id = spawn_challenger(db, user_id).id
    elif verdict == "retire_challenger":
        challenger.role = "retired"
        challenger.retired_at = now_utc()
        run.status = "completed"
        run.ended_at = now_utc()
        db.flush()
        new_challenger_id = spawn_challenger(db, user_id).id

    # Reflection (A2): each sleeve distils lessons from its own track record.
    lessons_created = 0
    if reflect:
        for strat, card in ((champion, champ_card), (challenger, chall_card)):
            if card is None or not card.n_resolved or strat.portfolio_id is None:
                continue
            decisions = _recent_decisions_payload(db, user_id, strat.portfolio_id)
            if decisions:
                lessons_created += len(
                    reflect_on_cycle(db, strat, card, decisions, llm_call=llm_call)
                )

    db.commit()
    summary = {
        "verdict": verdict,
        "champion_strategy_id": champion.id,
        "challenger_strategy_id": challenger.id,
        "champion_score": champ_score,
        "challenger_score": chall_score,
        "n_resolved": {"champion": n_champ, "challenger": n_chall},
        "min_resolved_required": min_resolved,
        "promoted_strategy_id": promoted_id,
        "new_challenger_id": new_challenger_id,
        "lessons_created": lessons_created,
        "cycles": {
            "champion": champ_cycle.get("status"),
            "challenger": chall_cycle.get("status"),
        },
        "competition_run_id": run.id,
    }
    logger.info("evolution round for %s: %s", user_id, verdict)
    return summary
