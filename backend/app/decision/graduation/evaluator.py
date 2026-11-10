"""The old graduation gate, kept for display (legacy since ADR 0025).

The capital ladder (``ladder.py``) decides what the satellite may hold. These
criteria are still computed and shown, each marked ``legacy``; nothing reads
the verdict. The Deflated Sharpe, PBO and minimum-track-record checks moved to
the lab (``app.lab.evidence_gate.admission``), where they screen
backtest-selected candidates; learning maturity is display only.


``evaluate_graduation`` is a pure read of persisted state — paper-portfolio
NAV snapshots, competition results, AI trades and reflection cycles — turned
into a multi-criterion verdict. It performs no LLM calls and writes nothing,
so it is cheap to call from an API handler or a scheduled job.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    AdvisorScorecard,
    CompetitionDecision,
    CompetitionRun,
    LlmPortfolioDecision,
    PaperPortfolio,
    PaperSnapshot,
    PaperTrade,
    PortfolioSnapshot,
    StrategyLesson,
    TrialLedgerEntry,
)
from app.foundation import quant_metrics as qm

_PERIODS_PER_YEAR = 252
_MOVED_TO_LAB = (
    "Moved to the lab (ADR 0025): it screens backtest-selected strategies before they may become "
    "ladder candidates. A forward paper record is judged by the capital ladder's e-processes."
)


@dataclass(frozen=True)
class GraduationConfig:
    """Thresholds for the graduation gate. Conservative by design."""

    min_observations: int = 60
    """Trading-day NAV snapshots required before significance is even testable."""
    min_dsr: float = 0.95
    """Deflated Sharpe Ratio required to reject the 'no skill' hypothesis."""
    max_drawdown: float = 0.25
    """Worst peak-to-trough loss tolerated on the paper portfolio."""
    min_outperformance: float = 0.0
    """Required total-return edge of paper over the real portfolio (fraction)."""
    min_decisions: int = 20
    """Independent AI buy/sell decisions required (decision sample size)."""
    min_learning_cycles: int = 5
    """Reflection / self-critique cycles required (learning maturity)."""

    # --- 4-axis sustained bar (PR2 D1) ---
    sustain_scorecards: int = 3
    """Consecutive most-recent scorecards that must ALL clear the axis floors."""
    min_axis_sharpe: float = 0.0
    """Axis 1 floor: Sharpe of the paper book over the scorecard window."""
    max_axis_brier: float = 0.25
    """Axis 2 ceiling: Brier score (0.25 = coin-flip at p=0.5)."""
    min_axis_mz_slope: float = 0.0
    """Axis 3 floor: Mincer-Zarnowitz slope must be positive (right direction)."""
    max_axis_drawdown: float = 0.25
    """Axis 4 ceiling: max drawdown within the scorecard window."""

    # --- ADR 0015 hard-gate conditions 3-5 ---
    max_pbo: float = 0.50
    """Ceiling on Probability of Backtest Overfitting (CSCV) across variants."""
    min_oos_observations: int = 20
    """NAV observations required strictly after the champion's trial-ledger
    registration — proof the track record includes data the strategy could
    not have been selected to fit."""


@dataclass
class GraduationCriterion:
    key: str
    label: str
    passed: bool
    progress: float  # 0..1
    value: float | None
    target: float | None
    detail: str
    blocking: bool = True
    # ADR 0025: every criterion here is legacy (display only).
    legacy: bool = True


@dataclass
class GraduationResult:
    graduated: bool
    overall_progress: float
    criteria: list[GraduationCriterion]
    metrics: dict[str, Any]
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the graduation result to a dictionary.
        
        Returns:
            A dictionary representation of the graduation result with graduated status, progress, criteria, metrics, timestamp, and mandatory safety contract flags.
        """
        return {
            "graduated": self.graduated,
            "overall_progress": round(self.overall_progress, 4),
            "criteria": [asdict(c) for c in self.criteria],
            "metrics": self.metrics,
            "generated_at": self.generated_at,
            # Honour the project-wide safety contract on every payload.
            "estimate": True,
            "not_tax_advice": True,
            "not_financial_advice": True,
        }


# --- helpers ---------------------------------------------------------------


def _nav_returns(snapshots: list[PaperSnapshot]) -> list[float]:
    """
    Compute period-over-period simple returns from NAV snapshots.
    
    Assumes snapshots are ordered chronologically. Filters out snapshots with no
    total_value, then calculates returns as (current / previous - 1.0) for
    consecutive NAV pairs where previous NAV is positive.
    
    Parameters:
    	snapshots (list[PaperSnapshot]): Snapshots in chronological order.
    
    Returns:
    	list[float]: Simple returns between consecutive NAV values.
    """
    navs = [float(s.total_value) for s in snapshots if s.total_value is not None]
    returns: list[float] = []
    for prev, cur in zip(navs, navs[1:]):
        if prev > 0:
            returns.append(cur / prev - 1.0)
    return returns


def _clamp01(x: float) -> float:
    """
    Constrain a value to the range [0.0, 1.0].
    
    Returns:
        float: The clamped value.
    """
    return max(0.0, min(1.0, x))


def _ai_portfolios(db: Session, user_id: str) -> list[PaperPortfolio]:
    """
    Retrieve the advisor loop's own paper portfolios (champion + challenger) for a user.

    Scoped by ``mandate`` (advisor / advisor-challenger), not ``managed_by ==
    "llm"`` — that field is also "llm" for the unrelated llm_portfolio mandate
    A/B sleeves, which would otherwise leak their review cycles, trades, and
    (in the no-champion-strategy fallback) candidacy into advisor's own
    graduation gate.

    Returns:
    	portfolios (list[PaperPortfolio]): The advisor champion/challenger portfolios.
    """
    from sqlalchemy import or_

    # "advisor" and the pre-ADR-0025 shared "advisor-challenger" sleeve, plus
    # each strategy's own "adv-<id>" sleeve since then.
    return (
        db.query(PaperPortfolio)
        .filter(
            PaperPortfolio.user_id == user_id,
            or_(
                PaperPortfolio.mandate.in_(["advisor", "advisor-challenger"]),
                PaperPortfolio.mandate.like("adv-%"),
            ),
        )
        .all()
    )


def _competition_win_rate(db: Session, portfolio_ids: set[str]) -> tuple[int, int]:
    """
    Count the number of competition wins and total decisions across the provided portfolios.
    
    Returns:
        tuple[int, int]: Win count and total competition decision count.
    """
    if not portfolio_ids:
        return 0, 0
    decisions = (
        db.query(CompetitionDecision)
        .filter(CompetitionDecision.portfolio_id.in_(portfolio_ids))
        .all()
    )
    wins = sum(1 for d in decisions if d.winner)
    return wins, len(decisions)


def _learning_cycles(db: Session, user_id: str, portfolio_ids: set[str]) -> int:
    """
    Count reflection cycles from completed portfolio reviews and self-critiques in competition runs.
    
    Returns:
        int: Total count of completed decision reviews and self-critique entries.
    """
    completed_reviews = 0
    if portfolio_ids:
        # Historic rows use "complete" (mandate reviews) while the advisor
        # cycle writes "completed" — count both.
        completed_reviews = (
            db.query(LlmPortfolioDecision)
            .filter(
                LlmPortfolioDecision.portfolio_id.in_(portfolio_ids),
                LlmPortfolioDecision.status.in_(["complete", "completed"]),
            )
            .count()
        )

    # PR2: persisted reflection lessons are learning cycles in the most
    # literal sense — the LLM distilled its own track record.
    reflections = (
        db.query(StrategyLesson).filter(StrategyLesson.user_id == user_id).count()
    )

    critiques = 0
    runs = (
        db.query(CompetitionRun)
        .filter(
            (CompetitionRun.portfolio_a_id.in_(portfolio_ids))
            | (CompetitionRun.portfolio_b_id.in_(portfolio_ids))
        )
        .all()
        if portfolio_ids
        else []
    )
    for run in runs:
        try:
            state = json.loads(run.state_json or "{}")
        except (ValueError, TypeError):
            continue
        for key in ("workspace_a", "workspace_b"):
            ws = state.get(key) or {}
            critiques += len(ws.get("self_critiques") or [])
    return completed_reviews + critiques + reflections


def _search_breadth(db: Session, portfolio_ids: set[str]) -> tuple[int, int, int]:
    """How many independent strategy *variants* the champion was selected from.

    The Deflated Sharpe Ratio (López de Prado, "The Deflated Sharpe Ratio")
    must discount for every trial in a best-of-N selection — but a "trial" is
    a distinct strategy/parameter variant evaluated during search, not every
    routine re-evaluation of the *same* strategy. A mandate review rebalances
    the champion's existing sleeve; it proposes no new variant, so it is not a
    trial. A competition round is a genuine best-of-two: each round pits the
    champion against a distinct challenger variant, so it counts.

    ``review_cycles`` is still returned (and logged) for observability, but it
    no longer feeds ``n_trials`` — counting it overstated the multiple-testing
    penalty and made the gate nearly unreachable in practice.

    Returns ``(review_cycles, competition_decisions, n_trials)`` where
    ``n_trials`` is the ledger-backed global count (Finding F15, ADR 0015):
    ``max(2, len(portfolio_ids), competition_decisions)`` here typically ran
    2-6, making the Deflated Sharpe a rounding error.
    """
    review_cycles = (
        db.query(LlmPortfolioDecision)
        .filter(
            LlmPortfolioDecision.portfolio_id.in_(portfolio_ids),
            # Historic rows use "complete" (mandate reviews) while the advisor
            # cycle writes "completed" — count both, matching _learning_cycles.
            LlmPortfolioDecision.status.in_(["complete", "completed"]),
        )
        .count()
        if portfolio_ids
        else 0
    )
    competition_decisions = (
        db.query(CompetitionDecision)
        .filter(CompetitionDecision.portfolio_id.in_(portfolio_ids))
        .count()
        if portfolio_ids
        else 0
    )
    # The ledger is the global count; the champion is also picked from these
    # portfolios and these competition rounds, so never deflate for fewer.
    n_trials = max(
        qm.resolve_n_trials(db, qm.ADVISOR_FAMILIES), len(portfolio_ids), competition_decisions
    )
    return review_cycles, competition_decisions, n_trials


def _real_total_return(db: Session, user_id: str) -> float | None:
    """
    Retrieve the user's most recent real portfolio total return as a decimal.
    
    Parameters:
        user_id (str): The user identifier.
    
    Returns:
        The total return as a decimal (e.g., 0.15 for 15%), or `None` if no snapshot exists or total return is unavailable.
    """
    snap = (
        db.query(PortfolioSnapshot)
        .filter(PortfolioSnapshot.user_id == user_id)
        .order_by(PortfolioSnapshot.date.desc())
        .first()
    )
    if snap is None or snap.total_return_pct is None:
        return None
    # total_return_pct is stored as a fraction by portfolio_service.py:
    # snapshot.total_return_pct = (current_total - base_value) / base_value
    # e.g. 0.05 means +5%.  No /100 conversion is needed.
    return float(snap.total_return_pct)


# --- main entry point ------------------------------------------------------


def evaluate_graduation(
    db: Session,
    user_id: str,
    config: GraduationConfig | None = None,
) -> GraduationResult:
    """
    Determines if an LLM-managed paper portfolio has earned permission to advise a real portfolio.
    
    Evaluates the highest-performing AI portfolio against seven criteria: track record length,
    statistical skill (deflated Sharpe ratio), out-of-sample consistency, drawdown limits,
    outperformance versus the real portfolio, decision sample size, and learning maturity. Produces
    a detailed report with pass/fail verdicts, progress metrics, and performance values for each
    criterion.
    
    Parameters:
        user_id (str): The user whose AI portfolio(s) to evaluate.
        config (GraduationConfig | None): Thresholds for each criterion. Defaults to
            GraduationConfig() if not provided.
    
    Returns:
        GraduationResult: Complete verdict including graduated flag, overall progress (0..1),
            criteria details with pass/fail status and reasoning, and performance metrics.
    """
    cfg = config or GraduationConfig()

    portfolios = _ai_portfolios(db, user_id)
    portfolio_ids = {p.id for p in portfolios}

    # Deflate for the full search breadth: every portfolio, mandate review and
    # competition decision the champion was selected from is a trial (False
    # Strategy Theorem), not just the number of portfolios.
    review_cycles, competition_decisions, n_trials = _search_breadth(db, portfolio_ids)

    # PR2 D1: the champion is the CURRENT champion strategy's sleeve — the
    # thing evolution actually promotes — not a best-of-N re-selection here.
    # Without a strategy row (legacy state), fall back to the most-skilful
    # AI portfolio by DSR.
    from app.decision.advisor.strategy import get_champion

    champion_strategy = get_champion(db, user_id)
    champion: PaperPortfolio | None = None
    champion_snaps: list[PaperSnapshot] = []
    champion_returns: list[float] = []

    if champion_strategy is not None and champion_strategy.portfolio_id:
        champion = db.get(PaperPortfolio, champion_strategy.portfolio_id)
        if champion is not None:
            champion_snaps = (
                db.query(PaperSnapshot)
                .filter(PaperSnapshot.portfolio_id == champion.id)
                .order_by(PaperSnapshot.date.asc())
                .all()
            )
            champion_returns = _nav_returns(champion_snaps)

    if champion is None:
        for p in portfolios:
            snaps = (
                db.query(PaperSnapshot)
                .filter(PaperSnapshot.portfolio_id == p.id)
                .order_by(PaperSnapshot.date.asc())
                .all()
            )
            rets = _nav_returns(snaps)
            if len(rets) < 3:
                continue
            # Without a champion strategy: the longest record (selecting by
            # Deflated Sharpe moved to the lab with the DSR itself, ADR 0025).
            if champion is None or len(rets) > len(champion_returns):
                champion = p
                champion_snaps = snaps
                champion_returns = rets

    criteria: list[GraduationCriterion] = []
    metrics: dict[str, Any] = {
        "n_ai_portfolios": len(portfolios),
        "n_trials": n_trials,
        "n_review_cycles": review_cycles,
        "n_competition_decisions": competition_decisions,
        "champion_portfolio_id": champion.id if champion else None,
        "champion_strategy_id": champion_strategy.id if champion_strategy else None,
    }

    # Track length is measured in daily NAV observations (snapshots); the
    # statistical gates operate on the N-1 returns derived from them.
    n_obs = len(champion_snaps)
    n_returns = len(champion_returns)

    # 1. Track length ------------------------------------------------------
    criteria.append(
        GraduationCriterion(
            key="track_length",
            label="Sufficient track record",
            passed=n_obs >= cfg.min_observations,
            progress=_clamp01(n_obs / cfg.min_observations),
            value=float(n_obs),
            target=float(cfg.min_observations),
            detail=f"{n_obs} of {cfg.min_observations} daily NAV observations.",
        )
    )

    # 2. Statistical skill (Deflated Sharpe Ratio) -------------------------
    psr = (
        qm.probabilistic_sharpe_ratio(champion_returns) if n_returns >= 3 else 0.0
    )
    sharpe = qm.sharpe_ratio(champion_returns) if n_returns >= 3 else 0.0
    sortino = qm.sortino_ratio(champion_returns) if n_returns >= 3 else 0.0
    metrics.update(
        {
            "dsr": None,
            "psr": round(psr, 4),
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "min_track_record_length": None,
            "observations": n_obs,
        }
    )
    criteria.append(
        GraduationCriterion(
            key="statistical_skill",
            label="Statistically significant skill (Deflated Sharpe)",
            passed=False,
            progress=0.0,
            value=None,
            target=cfg.min_dsr,
            detail=_MOVED_TO_LAB,
            blocking=False,
        )
    )

    # 3. Out-of-sample consistency (walk-forward halves) -------------------
    if n_returns >= 6:
        mid = n_returns // 2
        first_sr = qm.sharpe_ratio(champion_returns[:mid])
        second_sr = qm.sharpe_ratio(champion_returns[mid:])
        positive_halves = int(first_sr > 0) + int(second_sr > 0)
        consistency_pass = positive_halves == 2
        consistency_progress = positive_halves / 2.0
        consistency_detail = (
            f"Sharpe positive in {positive_halves}/2 halves "
            f"(first {first_sr:.2f}, second {second_sr:.2f})."
        )
    else:
        consistency_pass = False
        consistency_progress = 0.0
        consistency_detail = "Not enough history to test out-of-sample consistency."
    criteria.append(
        GraduationCriterion(
            key="consistency",
            label="Out-of-sample consistency",
            passed=consistency_pass,
            progress=consistency_progress,
            value=None,
            target=None,
            detail=consistency_detail,
        )
    )

    # 4. Drawdown ceiling --------------------------------------------------
    dd = qm.max_drawdown(champion_returns).get("max_drawdown", 0.0) if n_returns >= 1 else 0.0
    dd_abs = abs(dd)
    metrics["max_drawdown"] = round(dd_abs, 4)
    dd_progress = 1.0 if dd_abs <= cfg.max_drawdown else _clamp01(cfg.max_drawdown / dd_abs)
    criteria.append(
        GraduationCriterion(
            key="drawdown",
            label="Drawdown within ceiling",
            passed=n_returns >= 1 and dd_abs <= cfg.max_drawdown,
            progress=dd_progress if n_returns >= 1 else 0.0,
            value=round(dd_abs, 4),
            target=cfg.max_drawdown,
            detail=f"Max drawdown {dd_abs:.1%} vs {cfg.max_drawdown:.0%} ceiling.",
        )
    )

    # 5. Out-performance vs the real portfolio -----------------------------
    # total_return_pct is stored as a fraction by paper_portfolio.py:
    # total_return_pct = (total_value - baseline) / baseline
    # e.g. 0.18 means +18%.  No /100 conversion is needed.
    paper_return = (
        float(champion_snaps[-1].total_return_pct)
        if champion_snaps and champion_snaps[-1].total_return_pct is not None
        else None
    )
    real_return = _real_total_return(db, user_id)
    metrics["paper_total_return"] = (
        round(paper_return, 4) if paper_return is not None else None
    )
    metrics["real_total_return"] = (
        round(real_return, 4) if real_return is not None else None
    )
    if paper_return is not None and real_return is not None:
        edge = paper_return - real_return
        outperf_pass = edge >= cfg.min_outperformance
        outperf_progress = 1.0 if outperf_pass else _clamp01(0.5 + edge)
        outperf_detail = (
            f"Paper {paper_return:.1%} vs real {real_return:.1%} "
            f"(edge {edge:+.1%}, need ≥ {cfg.min_outperformance:.0%})."
        )
    else:
        edge = None
        outperf_pass = False
        outperf_progress = 0.0
        outperf_detail = "Need both paper and real portfolio returns to compare."
    metrics["outperformance_edge"] = round(edge, 4) if edge is not None else None
    criteria.append(
        GraduationCriterion(
            key="outperformance",
            label="Beats the real portfolio",
            passed=outperf_pass,
            progress=outperf_progress,
            value=round(edge, 4) if edge is not None else None,
            target=cfg.min_outperformance,
            detail=outperf_detail,
        )
    )

    # 6. Decision sample size ---------------------------------------------
    n_decisions = (
        db.query(PaperTrade)
        .filter(PaperTrade.portfolio_id.in_(portfolio_ids))
        .count()
        if portfolio_ids
        else 0
    )
    metrics["n_decisions"] = n_decisions
    criteria.append(
        GraduationCriterion(
            key="decision_sample",
            label="Enough independent decisions",
            passed=n_decisions >= cfg.min_decisions,
            progress=_clamp01(n_decisions / cfg.min_decisions),
            value=float(n_decisions),
            target=float(cfg.min_decisions),
            detail=f"{n_decisions} of {cfg.min_decisions} AI trades executed.",
        )
    )

    # 7. Learning maturity -------------------------------------------------
    cycles = _learning_cycles(db, user_id, portfolio_ids)
    metrics["learning_cycles"] = cycles
    criteria.append(
        GraduationCriterion(
            key="learning_maturity",
            label="Learning maturity (reflection cycles)",
            passed=cycles >= cfg.min_learning_cycles,
            progress=_clamp01(cycles / cfg.min_learning_cycles),
            value=float(cycles),
            target=float(cfg.min_learning_cycles),
            detail=f"{cycles} of {cfg.min_learning_cycles} reflection/self-critique cycles (display only).",
            blocking=False,
        )
    )

    # 8. Sustained 4-axis bar (PR2 D1) ------------------------------------
    # The champion's last N scorecards must ALL clear every axis floor — a
    # sustained window, not a lucky snapshot. Missing axes fail honestly.
    axis_cards: list[AdvisorScorecard] = (
        db.query(AdvisorScorecard)
        .filter(AdvisorScorecard.portfolio_id == champion.id)
        .order_by(AdvisorScorecard.window_end.desc())
        .limit(cfg.sustain_scorecards)
        .all()
        if champion is not None
        else []
    )
    axis_failures: list[str] = []
    missing_cards = max(0, cfg.sustain_scorecards - len(axis_cards))
    if missing_cards:
        axis_failures.append(
            f"only {len(axis_cards)} of {cfg.sustain_scorecards} scorecards exist"
        )
    card_failures = 0
    for card in axis_cards:
        tag = card.window_end.isoformat()
        checks = [
            (card.sharpe is None or card.sharpe < cfg.min_axis_sharpe,
             f"{tag}: sharpe {card.sharpe} < {cfg.min_axis_sharpe}"),
            (card.brier_avg is None or card.brier_avg > cfg.max_axis_brier,
             f"{tag}: brier {card.brier_avg} > {cfg.max_axis_brier}"),
            (card.mz_slope is None or card.mz_slope <= cfg.min_axis_mz_slope,
             f"{tag}: mz_slope {card.mz_slope} ≤ {cfg.min_axis_mz_slope}"),
            (card.max_drawdown is None or card.max_drawdown > cfg.max_axis_drawdown,
             f"{tag}: drawdown {card.max_drawdown} > {cfg.max_axis_drawdown}"),
        ]
        for failed_check, msg in checks:
            if failed_check:
                card_failures += 1
                axis_failures.append(msg)
    four_axis_pass = not axis_failures
    # A missing scorecard counts as all four axes failing — zero data must
    # read as zero progress, never as "almost there".
    n_axis_checks = cfg.sustain_scorecards * 4
    n_axis_fails = min(card_failures + missing_cards * 4, n_axis_checks)
    metrics["four_axis_failures"] = axis_failures[:12]
    criteria.append(
        GraduationCriterion(
            key="four_axis_bar",
            label="Sustained 4-axis bar (rolling scorecards)",
            passed=four_axis_pass,
            progress=1.0 if four_axis_pass else _clamp01(1.0 - n_axis_fails / n_axis_checks),
            value=float(len(axis_cards)),
            target=float(cfg.sustain_scorecards),
            detail=(
                f"All 4 axes clear their floors on the last "
                f"{cfg.sustain_scorecards} scorecards."
                if four_axis_pass
                else "; ".join(axis_failures[:4])
            ),
        )
    )

    # 9-10. PBO and the Minimum Track Record Length moved to the lab (ADR 0025).
    metrics["pbo"] = None
    for key, label, target in (
        ("backtest_overfitting", "Low probability of backtest overfitting (CSCV)", cfg.max_pbo),
        ("sufficient_track_record_length", "Observations clear the Minimum Track Record Length", None),
    ):
        criteria.append(
            GraduationCriterion(
                key=key, label=label, passed=False, progress=0.0, value=None, target=target,
                detail=_MOVED_TO_LAB, blocking=False,
            )
        )

    # 11. Out-of-sample confirmation since ledger registration — ADR 0015
    #     condition 5. The champion-promotion path (advisor/strategy.py)
    #     registers the trial; this criterion only reads it, keeping this
    #     function's "writes nothing" contract intact.
    oos_entry = (
        db.query(TrialLedgerEntry)
        .filter(
            TrialLedgerEntry.context == "advisor_champion",
            TrialLedgerEntry.trial_key == champion_strategy.id,
        )
        .one_or_none()
        if champion_strategy is not None
        else None
    )
    if oos_entry is None:
        oos_pass = False
        oos_progress = 0.0
        oos_n = 0
        oos_detail = "Champion has not been registered in the trial ledger yet."
    else:
        registered_at = oos_entry.registered_at
        registered_date = registered_at.date() if registered_at else None
        oos_n = sum(
            1 for s in champion_snaps if s.date and registered_date and s.date > registered_date
        )
        oos_pass = oos_n >= cfg.min_oos_observations
        oos_progress = _clamp01(oos_n / cfg.min_oos_observations)
        oos_detail = (
            f"{oos_n} of {cfg.min_oos_observations} NAV observations dated after "
            f"the champion's ledger registration ({registered_at.isoformat() if registered_at else '?'})."
        )
    metrics["oos_observations_since_registration"] = oos_n
    criteria.append(
        GraduationCriterion(
            key="out_of_sample_confirmation",
            label="Out-of-sample confirmation since registration",
            passed=oos_pass,
            progress=oos_progress,
            value=float(oos_n),
            target=float(cfg.min_oos_observations),
            detail=oos_detail,
        )
    )

    # Competition win-rate is informational, not a gate.
    wins, total_rounds = _competition_win_rate(db, portfolio_ids)
    metrics["competition_wins"] = wins
    metrics["competition_rounds"] = total_rounds
    metrics["competition_win_rate"] = (
        round(wins / total_rounds, 4) if total_rounds else None
    )

    blocking = [c for c in criteria if c.blocking]
    graduated = bool(blocking) and all(c.passed for c in blocking)
    shown = [c for c in criteria if c.detail != _MOVED_TO_LAB]
    overall = sum(c.progress for c in shown) / len(shown) if shown else 0.0

    return GraduationResult(
        graduated=graduated,
        overall_progress=overall,
        criteria=criteria,
        metrics=metrics,
    )
