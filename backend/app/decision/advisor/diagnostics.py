"""End-to-end health check for the autonomous advisor / paper-portfolio loop.

The loop is a chain of nine stages, and a break anywhere upstream makes every
stage below it silently idle:

    scheduler → discover candidates → price history → deterministic proposal
    → risk gate → LLM decision → paper trade → prediction ledger → scorecard
    → evolution → graduation

Historically a break produced *no output at all* (``run_advisor_cycle``
returned early on ``no_candidates`` without writing a decision row), so the UI
kept showing the last successful cycle and the loop looked alive while doing
nothing for weeks. This module inspects each stage against the same thresholds
the production code enforces and reports where the chain is actually severed.

Every check is read-only and DB-local. The LLM endpoint probe is opt-in
(``probe_llm=True``) because it makes a real network call.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    AdvisorScorecard,
    AdvisorStrategy,
    DiscoverCandidate,
    DiscoveryPrediction,
    DiscoverRun,
    JobRun,
    LlmPortfolioDecision,
    PaperHolding,
    PaperPortfolio,
    PaperSnapshot,
    PaperTrade,
    StrategyLesson,
)
from app.foundation.models.entities._core import now_utc

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"

_SEVERITY = {OK: 0, SKIPPED: 1, WARN: 2, FAIL: 3}

# Jobs that must fire for the loop to advance, with their expected cadence in
# hours. A run older than ``max_age_hours`` means the scheduler is not driving
# that stage (worker down, job never registered, or APScheduler wedged).
_LOOP_JOBS: dict[str, tuple[str, float]] = {
    "discover_refresh": ("Weekly Discover candidate refresh", 200.0),
    "advisor_cycle": ("Daily advisor paper-trade cycle", 96.0),
    "evolution_round": ("Champion-vs-challenger evolution round", 96.0),
    "snapshot_paper_portfolios": ("Daily NAV snapshot", 48.0),
    "discovery_resolution": ("Nightly prediction resolution", 48.0),
    "price_refresh": ("Price cache refresh", 6.0),
}

# Mirrors of the thresholds enforced elsewhere. Kept as imports where cheap so
# diagnostics can never drift from the code they describe.
_MIN_HISTORY_ROWS = 60  # advisor.decision._price_series floor
_HISTORY_LOOKBACK_DAYS = 365 * 2  # advisor.decision._HISTORY_DAYS
_MIN_COMPOSITE_AXES = 3  # advisor.evolution.composite_score
_MIN_MAGNITUDE_PAIRS = 3  # quant_metrics.compute_mincer_zarnowitz
_MIN_NAV_RETURNS = 5  # advisor.scorecard axes 1+4
_MIN_METRIC_SNAPSHOTS = 20  # paper_portfolio.compute_metrics
_DISCOVER_STALE_DAYS = 7  # advisor.cycle._DISCOVER_STALE_DAYS
_CYCLE_LOOKBACK_DAYS = 14
# How many of the most recent trading days decide whether a failure is *current*.
# Two schedulers plus manual runs mean a bad day writes up to
# ``_MAX_FAILED_ATTEMPTS_PER_DAY`` failed rows, so a burst of failures that has
# since been fixed kept a 14-day tally red long after the loop recovered. What
# matters is whether the loop is failing *now*.
_RECENT_TRADING_DAYS = 3


@dataclass
class Check:
    """One diagnostic stage result."""

    key: str
    label: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    remedy: str | None = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    """UTC-normalised ISO timestamp, or None."""
    stamped = _aware(value)
    return stamped.isoformat() if stamped is not None else None


def _aware_all(values: list[datetime | None]) -> list[datetime]:
    """Drop Nones and normalise the rest, so min/max are total orders."""
    return [stamped for stamped in (_aware(v) for v in values) if stamped is not None]


def _age_hours(value: datetime | None, *, ref: datetime) -> float | None:
    stamped = _aware(value)
    if stamped is None:
        return None
    return (ref - stamped).total_seconds() / 3600.0


def _market_holidays(db: Session) -> set[str]:
    """Holiday set the advisor cron itself honours."""
    from app.foundation.settings import get_public_settings

    try:
        raw = str(
            get_public_settings(db).get("advisor_market_holidays")
            or os.getenv("ADVISOR_MARKET_HOLIDAYS")
            or ""
        )
    except Exception:
        raw = os.getenv("ADVISOR_MARKET_HOLIDAYS") or ""
    return {h.strip() for h in raw.split(",") if h.strip()}


def _expected_trading_days(start: date, end: date, holidays: set[str]) -> list[date]:
    """Weekdays in [start, end] that are not configured market holidays."""
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor.isoformat() not in holidays:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Stage checks
# ---------------------------------------------------------------------------


def _check_scheduler(db: Session, ref: datetime) -> Check:
    """Are the cron jobs that drive the loop actually firing?"""
    rows: dict[str, Any] = {}
    stale: list[str] = []
    errored: list[str] = []
    never_ran: list[str] = []

    for job_name, (label, max_age) in _LOOP_JOBS.items():
        last = (
            db.query(JobRun)
            .filter(JobRun.job_name == job_name)
            .order_by(JobRun.started_at.desc())
            .first()
        )
        if last is None:
            never_ran.append(job_name)
            rows[job_name] = {"label": label, "last_run": None, "status": None, "age_hours": None}
            continue
        age = _age_hours(last.started_at, ref=ref)
        rows[job_name] = {
            "label": label,
            "last_run": _iso(last.started_at),
            "status": last.status,
            "age_hours": round(age, 1) if age is not None else None,
            "error": last.error_message,
            "max_age_hours": max_age,
        }
        if age is not None and age > max_age:
            stale.append(job_name)
        if last.status not in ("success", "running"):
            errored.append(job_name)

    if never_ran and len(never_ran) == len(_LOOP_JOBS):
        return Check(
            key="scheduler",
            label="Scheduler",
            status=FAIL,
            detail=(
                "No JobRun rows exist for any loop job — the quantfolio-worker "
                "process has never run, or is not the process registering these jobs."
            ),
            data={"jobs": rows},
            remedy="Check `systemctl status quantfolio-worker` and its logs on the app LXC.",
        )
    if never_ran or stale or errored:
        parts = []
        if never_ran:
            parts.append(f"never ran: {', '.join(sorted(never_ran))}")
        if stale:
            parts.append(f"stale: {', '.join(sorted(stale))}")
        if errored:
            parts.append(f"last run failed: {', '.join(sorted(errored))}")
        return Check(
            key="scheduler",
            label="Scheduler",
            status=FAIL if (never_ran or errored) else WARN,
            detail="; ".join(parts),
            data={"jobs": rows},
            remedy="Restart quantfolio-worker and re-check; inspect JobRun.error_message.",
        )
    return Check(
        key="scheduler",
        label="Scheduler",
        status=OK,
        detail="All loop jobs ran within their expected cadence.",
        data={"jobs": rows},
    )


def _sleeve_state(db: Session, portfolio: PaperPortfolio | None) -> dict[str, Any]:
    if portfolio is None:
        return {}
    from app.decision.paper_portfolio import _current_cash_balance, get_summary

    holdings = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio.id).count()
    trades = db.query(PaperTrade).filter(PaperTrade.portfolio_id == portfolio.id).count()
    snapshots = db.query(PaperSnapshot).filter(PaperSnapshot.portfolio_id == portfolio.id).count()
    try:
        cash = float(_current_cash_balance(portfolio, db))
    except Exception:
        cash = float(portfolio.initial_cash or 0)
    state: dict[str, Any] = {
        "portfolio_id": portfolio.id,
        "name": portfolio.name,
        "mandate": portfolio.mandate,
        "initial_cash": float(portfolio.initial_cash or 0),
        "cash_balance": round(cash, 2),
        "holdings": holdings,
        "trades": trades,
        "snapshots": snapshots,
    }
    try:
        summary = get_summary(db, portfolio.id)
        state["total_value"] = summary.get("total_value")
        state["securities_value"] = summary.get("securities_value")
        state["total_return_pct"] = summary.get("total_return_pct")
    except Exception as exc:  # pragma: no cover - defensive
        state["summary_error"] = str(exc)
    return state


def _check_sleeves(db: Session, user_id: str) -> Check:
    """Do the champion and challenger paper sleeves exist and hold anything?"""
    from app.decision.advisor.strategy import get_active_challenger, get_champion

    champion = get_champion(db, user_id)
    challenger = get_active_challenger(db, user_id)

    champ_pf = db.get(PaperPortfolio, champion.portfolio_id) if champion and champion.portfolio_id else None
    chall_pf = (
        db.get(PaperPortfolio, challenger.portfolio_id)
        if challenger and challenger.portfolio_id
        else None
    )

    data = {
        "champion": {
            "strategy_id": champion.id if champion else None,
            "sleeve": _sleeve_state(db, champ_pf),
        },
        "challenger": {
            "strategy_id": challenger.id if challenger else None,
            "sleeve": _sleeve_state(db, chall_pf),
        },
    }

    if champion is None or champ_pf is None:
        return Check(
            key="sleeves",
            label="Paper sleeves",
            status=FAIL,
            detail="No champion strategy or backing paper portfolio exists yet.",
            data=data,
            remedy="Trigger one cycle from the Advisor Loop tab — the first run seeds the sleeve.",
        )
    if challenger is None or chall_pf is None:
        return Check(
            key="sleeves",
            label="Paper sleeves",
            status=WARN,
            detail="Champion exists but there is no active challenger — evolution cannot compare sleeves.",
            data=data,
            remedy="Run one evolution round; it spawns a challenger when none is active.",
        )
    return Check(
        key="sleeves",
        label="Paper sleeves",
        status=OK,
        detail=(
            f"Champion sleeve: {data['champion']['sleeve'].get('holdings', 0)} holdings, "
            f"{data['champion']['sleeve'].get('trades', 0)} trades. "
            f"Challenger sleeve: {data['challenger']['sleeve'].get('holdings', 0)} holdings, "
            f"{data['challenger']['sleeve'].get('trades', 0)} trades."
        ),
        data=data,
    )


def _check_candidate_feed(db: Session, user_id: str, ref: datetime) -> Check:
    """Is there a fresh Discover run with shortlisted candidates to trade on?

    ``run_advisor_cycle`` aborts with ``no_candidates`` when this is empty, so
    a missing or stale Discover run starves every downstream stage.
    """
    run = (
        db.query(DiscoverRun)
        .filter(DiscoverRun.user_id == user_id, DiscoverRun.status == "completed")
        .order_by(DiscoverRun.created_at.desc())
        .first()
    )
    if run is None:
        any_run = (
            db.query(DiscoverRun)
            .filter(DiscoverRun.user_id == user_id)
            .order_by(DiscoverRun.created_at.desc())
            .first()
        )
        return Check(
            key="candidate_feed",
            label="Discover candidate feed",
            status=FAIL,
            detail=(
                "No completed Discover run exists. The advisor cycle exits with "
                "'no_candidates' every day and never reaches the LLM."
                + (f" Newest run is status='{any_run.status}'." if any_run else "")
            ),
            data={"latest_run": None, "newest_any_status": any_run.status if any_run else None},
            remedy="Run Discover (or schedule it) so the cycle has a shortlist to trade.",
        )

    age_days = (_age_hours(run.created_at, ref=ref) or 0.0) / 24.0
    shortlisted = (
        db.query(DiscoverCandidate)
        .filter(DiscoverCandidate.run_id == run.id, DiscoverCandidate.status == "shortlisted")
        .all()
    )
    symbols = [c.symbol for c in shortlisted if c.symbol]
    data = {
        "latest_run": {
            "id": run.id,
            "created_at": _iso(run.created_at),
            "age_days": round(age_days, 1),
            "shortlisted": len(symbols),
            "symbols": symbols[:25],
        },
        "stale_after_days": _DISCOVER_STALE_DAYS,
    }
    if not symbols:
        return Check(
            key="candidate_feed",
            label="Discover candidate feed",
            status=FAIL,
            detail=(
                f"Newest completed Discover run ({age_days:.0f}d old) shortlisted 0 candidates — "
                "the cycle exits with 'no_candidates'."
            ),
            data=data,
            remedy="Re-run Discover; check its reject_stage breakdown for why everything was filtered out.",
        )
    if age_days > _DISCOVER_STALE_DAYS:
        return Check(
            key="candidate_feed",
            label="Discover candidate feed",
            status=WARN,
            detail=(
                f"Candidate shortlist is {age_days:.0f} days old (stale after {_DISCOVER_STALE_DAYS}d). "
                "The cycle keeps re-proposing the same frozen names, so target weights are already "
                "met and no new trade is generated."
            ),
            data=data,
            remedy="Schedule Discover to refresh the shortlist on a cadence.",
        )
    return Check(
        key="candidate_feed",
        label="Discover candidate feed",
        status=OK,
        detail=f"{len(symbols)} shortlisted candidates from a run {age_days:.1f} days old.",
        data=data,
    )


def _check_price_history(db: Session, user_id: str, symbols: list[str]) -> Check:
    """Do candidate/holding symbols have enough cached history to be evaluable?

    ``advisor.decision._price_series`` drops any symbol with fewer than 60
    closes, and the optimiser is skipped below 2 evaluable symbols. Note the
    price backfill job only covers ``Holding`` and synced-position tickers —
    Discover candidates are not backfilled, so they routinely arrive bare.
    """
    from app.foundation.market import history as market_history

    checked: list[dict[str, Any]] = []
    evaluable = 0
    for symbol in symbols[:40]:
        try:
            rows = market_history(db, symbol, days=_HISTORY_LOOKBACK_DAYS, allow_live=False)
        except Exception as exc:  # pragma: no cover - defensive
            checked.append({"symbol": symbol, "rows": 0, "evaluable": False, "error": str(exc)})
            continue
        closes = [r for r in rows if r.get("close") is not None]
        ok = len(closes) >= _MIN_HISTORY_ROWS
        evaluable += 1 if ok else 0
        checked.append({"symbol": symbol, "rows": len(closes), "evaluable": ok})

    missing = [c["symbol"] for c in checked if not c["evaluable"]]
    data = {
        "min_rows_required": _MIN_HISTORY_ROWS,
        "checked": checked,
        "evaluable": evaluable,
        "not_evaluable": missing,
    }
    if not checked:
        return Check(
            key="price_history",
            label="Price history",
            status=SKIPPED,
            detail="No symbols to check (no candidates and no holdings).",
            data=data,
        )
    if evaluable < 2:
        return Check(
            key="price_history",
            label="Price history",
            status=FAIL,
            detail=(
                f"Only {evaluable} of {len(checked)} symbols have ≥{_MIN_HISTORY_ROWS} cached closes. "
                "The optimiser is skipped below 2 evaluable symbols and the proposal degrades to "
                "an equal-weight fallback or nothing at all."
            ),
            data=data,
            remedy="Backfill history for candidate symbols, not just holdings and DKB positions.",
        )
    if missing:
        return Check(
            key="price_history",
            label="Price history",
            status=WARN,
            detail=(
                f"{len(missing)} of {len(checked)} symbols lack ≥{_MIN_HISTORY_ROWS} cached closes "
                f"and are silently excluded from the proposal: {', '.join(missing[:8])}"
                + ("…" if len(missing) > 8 else "")
            ),
            data=data,
            remedy="Extend the price backfill to cover Discover candidates and paper holdings.",
        )
    return Check(
        key="price_history",
        label="Price history",
        status=OK,
        detail=f"All {len(checked)} symbols have ≥{_MIN_HISTORY_ROWS} cached closes.",
        data=data,
    )


def _llm_failure_evidence(notes: dict[str, Any]) -> dict[str, Any]:
    """Pull the decision-stage evidence out of a cycle's stored payload.

    ``llm_attempts`` carries the endpoint's own account of each call. Without it
    the panel could only repeat the cycle's error string, and every distinct
    decision-stage failure — prose, an unconstrained fallback, a completion cut
    off at the token limit — reads as "not valid JSON".
    """
    attempts = notes.get("llm_attempts")
    evidence: dict[str, Any] = {}
    if isinstance(attempts, list) and attempts:
        evidence["llm_attempts"] = attempts
        finishes = [a.get("finish_reason") for a in attempts if isinstance(a, dict)]
        if any(f == "length" for f in finishes):
            evidence["diagnosis"] = (
                "the completion hit the token limit and was cut off — the JSON was "
                "incomplete, not wrong"
            )
        elif any(isinstance(a, dict) and a.get("schema_dropped") for a in attempts):
            evidence["diagnosis"] = (
                "the server rejected the JSON schema, so generation ran unconstrained"
            )
    raw = notes.get("llm_raw_responses")
    if isinstance(raw, list) and raw:
        evidence["llm_response_preview"] = str(raw[-1])[:600]
    return evidence


def _check_cycle_history(
    db: Session,
    portfolio_id: str | None,
    ref: datetime,
    holidays: set[str],
    *,
    mandate: str | None = None,
) -> Check:
    """Did a cycle actually run on each recent trading day, and with what outcome?

    A trading day with *no* decision row at all is the silent-failure signature:
    the cycle bailed out before writing anything, so the UI kept showing the
    previous successful run.

    Outcomes are judged **per trading day**, not per row. A failed cycle is not
    terminal, so every trigger that day re-runs it — the 10:00 advisor job, the
    10:30 evolution round, any manual run — up to
    ``cycle._MAX_FAILED_ATTEMPTS_PER_DAY``. Counting rows therefore multiplied
    one bad day into three failures and reported "16 of 19 runs failed" over a
    window of 11 days, which is not a number anyone can act on.
    """
    if portfolio_id is None:
        return Check(
            key="cycle_history",
            label="Cycle history",
            status=SKIPPED,
            detail="No champion sleeve yet.",
        )

    window_start = (ref - timedelta(days=_CYCLE_LOOKBACK_DAYS)).date()
    # Align the query to the start of the window's first *day*. Filtering on the
    # raw ``ref - 14d`` timestamp excluded records stamped earlier that same day,
    # while ``expected`` still counted the day — so the oldest day in the window
    # was reported silent whenever it happened to be a weekday.
    window_start_dt = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)
    query = db.query(LlmPortfolioDecision).filter(
        LlmPortfolioDecision.portfolio_id == portfolio_id,
        LlmPortfolioDecision.review_date >= window_start_dt,
    )
    if mandate:
        # The weekly mandate review writes its own rows against the same
        # portfolio; without this filter its statuses were tallied as advisor
        # cycles and the counts could not be reconciled with the trade log.
        query = query.filter(LlmPortfolioDecision.mandate == mandate)
    rows = query.order_by(LlmPortfolioDecision.review_date.desc()).all()

    by_day: dict[str, str] = {}
    attempts_by_day: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for row in rows:
        stamped = _aware(row.review_date)
        if stamped is None:
            continue
        day = stamped.date().isoformat()
        # Rows arrive newest-first, so the first one seen is that day's outcome.
        by_day.setdefault(day, row.status or "unknown")
        attempts_by_day[day] = attempts_by_day.get(day, 0) + 1
        statuses[row.status or "unknown"] = statuses.get(row.status or "unknown", 0) + 1

    expected = _expected_trading_days(window_start, ref.date(), holidays)
    silent = [d.isoformat() for d in expected if d.isoformat() not in by_day]

    recorded_days = sorted(by_day)
    failed_days = [d for d in recorded_days if by_day[d] == "failed"]
    recent_days = recorded_days[-_RECENT_TRADING_DAYS:]
    recent_failed = [d for d in recent_days if by_day[d] == "failed"]

    last_row = rows[0] if rows else None
    last_notes: dict[str, Any] = {}
    if last_row is not None:
        try:
            last_notes = json.loads(last_row.decision_json or "{}")
        except json.JSONDecodeError:
            last_notes = {}

    # The newest row is usually a healthy one, so a window full of failures was
    # reported as a bare status tally with the reason nowhere on screen. Carry
    # the most recent failure's error text so the panel names the blocker.
    last_failure = next((r for r in rows if r.status == "failed"), None)
    failure_notes: dict[str, Any] = {}
    if last_failure is not None:
        try:
            failure_notes = json.loads(last_failure.decision_json or "{}")
        except json.JSONDecodeError:
            failure_notes = {}
    failure_age_days = (
        round((_age_hours(last_failure.review_date, ref=ref) or 0.0) / 24.0, 1)
        if last_failure is not None
        else None
    )

    last_failure_payload: dict[str, Any] | None = (
        {
            "review_date": _iso(last_failure.review_date),
            "error": last_failure.error,
            "days_ago": failure_age_days,
            **_llm_failure_evidence(failure_notes),
        }
        if last_failure is not None
        else None
    )

    data: dict[str, Any] = {
        "window_days": _CYCLE_LOOKBACK_DAYS,
        "mandate": mandate,
        "expected_trading_days": len(expected),
        "days_with_a_record": len(by_day),
        "silent_days": silent,
        "status_counts": statuses,
        # Rows are attempts, not days: a failed cycle is retried by every
        # trigger that day, so these two numbers differ by design.
        "total_runs": len(rows),
        "day_outcomes": by_day,
        "attempts_per_day": attempts_by_day,
        "failed_days": failed_days,
        "recent_days_checked": recent_days,
        "recent_failed_days": recent_failed,
        "last_record": {
            "review_date": _iso(last_row.review_date) if last_row else None,
            "status": last_row.status if last_row else None,
            "error": last_row.error if last_row else None,
            "optimizer_status": last_notes.get("optimizer_status"),
            "notes": last_notes.get("notes"),
            "blocked": last_notes.get("blocked"),
            "errors": last_notes.get("errors"),
        }
        if last_row
        else None,
        "last_failure": last_failure_payload,
    }

    if not rows:
        return Check(
            key="cycle_history",
            label="Cycle history",
            status=FAIL,
            detail=(
                f"No cycle record in the last {_CYCLE_LOOKBACK_DAYS} days across "
                f"{len(expected)} trading days. The cycle is either not firing or is "
                "returning before it writes an audit row."
            ),
            data=data,
            remedy="Check the advisor_cycle JobRun rows and the worker log for early-exit reasons.",
        )
    if silent:
        return Check(
            key="cycle_history",
            label="Cycle history",
            status=FAIL if len(silent) > len(expected) / 2 else WARN,
            detail=(
                f"{len(silent)} of {len(expected)} trading days have no cycle record at all "
                f"(silent no-op). Recorded statuses: {statuses}."
            ),
            data=data,
            remedy="Early-exit paths must persist a heartbeat record so idle days are visible.",
        )
    if failed_days:
        reason = (last_failure.error if last_failure else None) or "no error text recorded"
        if len(reason) > 240:
            reason = reason[:237] + "…"
        diagnosis = (last_failure_payload or {}).get("diagnosis")
        if diagnosis:
            reason = f"{reason} — {diagnosis}"
        if not recent_failed:
            # Historical failures only. Leaving these as a block meant a bug
            # fixed on Thursday still read as "the loop is broken" the
            # following Tuesday, and buried whatever was actually wrong.
            return Check(
                key="cycle_history",
                label="Cycle history",
                status=WARN,
                detail=(
                    f"{len(failed_days)} of {len(by_day)} trading days failed, but the "
                    f"{len(recent_days)} most recent cycles are healthy — the last failure "
                    f"was {failure_age_days:.0f} days ago. Kept as history, not a current "
                    f"blocker. Most recent failure: {reason}"
                ),
                data=data,
                remedy=(
                    "Nothing to fix upstream unless the failures return; the older rows "
                    "stay visible so a recurrence is recognisable."
                ),
            )
        return Check(
            key="cycle_history",
            label="Cycle history",
            status=FAIL if len(recent_failed) > len(recent_days) / 2 else WARN,
            detail=(
                f"{len(recent_failed)} of the {len(recent_days)} most recent trading days "
                f"failed ({len(failed_days)} of {len(by_day)} across the window, from "
                f"{len(rows)} runs — a failed day is retried by every trigger). "
                f"Most recent failure: {reason}"
            ),
            data=data,
            remedy=(
                "A cycle only fails when the model returned nothing usable. Read "
                "last_failure: 'finish_reason: length' in llm_attempts means the completion "
                "was cut off and the budget is the fix; 'outside risk-gate-passing set' or "
                "'non-hold without thesis' means the prompt is."
            ),
        )
    return Check(
        key="cycle_history",
        label="Cycle history",
        status=OK,
        detail=f"A record exists for every trading day. Statuses: {statuses}.",
        data=data,
    )


def _check_trade_activity(
    db: Session,
    portfolio_id: str | None,
    ref: datetime,
    *,
    upstream_broken: str | None = None,
    skip_reasons: dict[str, int] | None = None,
) -> Check:
    """How long since the sleeve last actually traded, and why?

    Args:
        upstream_broken: Label of a failing earlier stage, when there is one. A
            sleeve cannot trade on decisions it never received, so an idle book
            downstream of a broken decision stage is a *consequence* — reporting
            it as its own blocker sent the reader looking for a second fault
            that does not exist.
        skip_reasons: Aggregated ``skipped`` reasons from the latest cycle. These
            name the binding constraint per order, which is the difference
            between "nothing was proposed" and "everything proposed was
            already at target".
    """
    if portfolio_id is None:
        return Check(
            key="trade_activity",
            label="Trade activity",
            status=SKIPPED,
            detail="No champion sleeve yet.",
        )

    trades = (
        db.query(PaperTrade)
        .filter(PaperTrade.portfolio_id == portfolio_id)
        .order_by(PaperTrade.date.desc())
        .limit(10)
        .all()
    )
    total = db.query(PaperTrade).filter(PaperTrade.portfolio_id == portfolio_id).count()
    last_age_days = None
    if trades:
        age = _age_hours(trades[0].date, ref=ref)
        last_age_days = round((age or 0.0) / 24.0, 1)

    reasons = dict(skip_reasons or {})
    dominant = max(reasons, key=lambda k: reasons[k]) if reasons else None
    data = {
        "total_trades": total,
        "days_since_last_trade": last_age_days,
        "latest_cycle_skip_reasons": reasons,
        "upstream_blocker": upstream_broken,
        "recent": [
            {
                "ticker": t.ticker,
                "side": t.side,
                "value": float(t.value),
                "date": _iso(t.date),
            }
            for t in trades
        ],
    }

    # Why nothing reached the book, in the words of whichever stage stopped it.
    if upstream_broken:
        cause = (
            f"Nothing reached the book because '{upstream_broken}' is failing — no usable "
            "decisions were produced to execute."
        )
    elif dominant:
        cause = f"Every proposed order was skipped; the most common reason was: {dominant}"
    else:
        cause = (
            "The latest cycle recorded no skipped orders either, so the decision stage "
            "proposed nothing to execute."
        )

    if total == 0:
        return Check(
            key="trade_activity",
            label="Trade activity",
            status=WARN if upstream_broken else FAIL,
            detail=f"The sleeve has never executed a paper trade. {cause}",
            data=data,
            remedy=(
                f"Fix '{upstream_broken}' first — this stage cannot recover on its own."
                if upstream_broken
                else "Work upstream: candidate feed → price history → LLM decision."
            ),
        )
    if last_age_days is not None and last_age_days > 7:
        # Downstream of a break this is not an independent failure, and marking
        # it as one is what turned a single root cause into four red stages.
        status = WARN if upstream_broken else (FAIL if last_age_days > 14 else WARN)
        return Check(
            key="trade_activity",
            label="Trade activity",
            status=status,
            detail=(
                f"{total} trade(s) total; the last one was {last_age_days:.0f} days ago. "
                f"{cause}"
            ),
            data=data,
            remedy=(
                f"Fix '{upstream_broken}' first; trade flow resumes from there."
                if upstream_broken
                else (
                    "Open Paper Portfolios → Advisor Loop and read the 'Not executed' panel; "
                    "if every reason is 'target already met', the candidate shortlist is stale."
                )
            ),
        )
    return Check(
        key="trade_activity",
        label="Trade activity",
        status=OK,
        detail=f"{total} trades; last one {last_age_days} days ago.",
        data=data,
    )


def _check_prediction_ledger(db: Session, user_id: str, portfolio_ids: list[str], ref: datetime) -> Check:
    """Are prediction rows accumulating and maturing?

    Predictions are the *only* feedback signal the loop learns from, and one is
    written per executed trade — so no trades means no learning, ever.
    """
    query = db.query(DiscoveryPrediction).filter(DiscoveryPrediction.user_id == user_id)
    if portfolio_ids:
        query = query.filter(DiscoveryPrediction.portfolio_id.in_(portfolio_ids))
    rows = query.all()

    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.outcome_status or "unknown"] = by_status.get(row.outcome_status or "unknown", 0) + 1

    pending = [r for r in rows if r.outcome_status == "pending"]
    pending_resolve_at = _aware_all([r.resolve_at for r in pending])
    next_resolve = min(pending_resolve_at, default=None)
    overdue = [
        r
        for r in pending
        if (stamped := _aware(r.resolve_at)) is not None and stamped <= ref
    ]

    data = {
        "total": len(rows),
        "by_status": by_status,
        "resolved": by_status.get("resolved", 0),
        "pending": len(pending),
        "overdue_pending": len(overdue),
        "next_resolve_at": next_resolve.isoformat() if next_resolve else None,
        # Rows inside their horizon with nothing overdue: the resolution job is
        # keeping up and the only thing missing is elapsed time.
        "waiting_on_horizon": bool(pending) and not overdue and not by_status.get("resolved"),
    }

    if not rows:
        return Check(
            key="prediction_ledger",
            label="Prediction ledger",
            status=FAIL,
            detail=(
                "No prediction rows for these sleeves. Predictions are written only for "
                "executed trades, so the learning loop has no input at all."
            ),
            data=data,
            remedy="Fix trade execution upstream; consider logging holds as predictions too.",
        )
    if overdue:
        return Check(
            key="prediction_ledger",
            label="Prediction ledger",
            status=WARN,
            detail=f"{len(overdue)} prediction(s) are past resolve_at but still pending — the resolution job is behind.",
            data=data,
            remedy="Check the discovery_resolution JobRun; unresolved rows never reach the scorecard.",
        )
    return Check(
        key="prediction_ledger",
        label="Prediction ledger",
        status=OK if by_status.get("resolved") else WARN,
        detail=(
            f"{len(rows)} prediction(s): {by_status.get('resolved', 0)} resolved, "
            f"{len(pending)} pending"
            + (f", next matures {next_resolve.date().isoformat()}" if next_resolve else "")
            + "."
        ),
        data=data,
    )


def _latest_skip_reasons(
    db: Session,
    portfolio_id: str | None,
    mandate: str | None,
) -> dict[str, int]:
    """Aggregate the ``skipped`` reasons from the sleeve's most recent cycle.

    The reasons are already written into every cycle's audit row; they were just
    never surfaced, so "the sleeve has not traded in 20 days" arrived with no
    indication of which constraint bound.
    """
    if portfolio_id is None:
        return {}
    query = db.query(LlmPortfolioDecision.decision_json).filter(
        LlmPortfolioDecision.portfolio_id == portfolio_id
    )
    if mandate:
        query = query.filter(LlmPortfolioDecision.mandate == mandate)
    row = query.order_by(LlmPortfolioDecision.review_date.desc()).first()
    if row is None:
        return {}
    try:
        payload = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return {}
    counts: dict[str, int] = {}
    for entry in payload.get("skipped") or []:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason") or "unspecified")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _pending_horizon(
    db: Session,
    user_id: str,
    portfolio_ids: list[str],
    ref: datetime,
) -> dict[str, Any]:
    """When, if ever, will these sleeves have a resolved prediction?

    Calibration and magnitude cannot exist before the 21-trading-day horizon
    elapses, and neither can a reflection lesson. Without this the two stages
    were reported as blockers on day one of a fresh sleeve, which is a fact
    about the calendar rather than a defect — and it buried the one stage that
    was genuinely broken.
    """
    if not portfolio_ids:
        return {"pending": 0, "overdue": 0, "next_resolve_at": None, "days_to_next": None}

    rows = (
        db.query(DiscoveryPrediction.resolve_at, DiscoveryPrediction.outcome_status)
        .filter(
            DiscoveryPrediction.user_id == user_id,
            DiscoveryPrediction.portfolio_id.in_(portfolio_ids),
            DiscoveryPrediction.outcome_status == "pending",
        )
        .all()
    )
    stamps = _aware_all([resolve_at for resolve_at, _ in rows])
    future = [s for s in stamps if s > ref]
    overdue = [s for s in stamps if s <= ref]
    next_resolve = min(future, default=None)
    return {
        "pending": len(rows),
        "overdue": len(overdue),
        "next_resolve_at": next_resolve.isoformat() if next_resolve else None,
        "days_to_next": (
            round((next_resolve - ref).total_seconds() / 86400.0, 1) if next_resolve else None
        ),
    }


def _axis_availability(card: AdvisorScorecard | None) -> dict[str, bool]:
    if card is None:
        return {"risk_adjusted": False, "calibration": False, "magnitude": False, "downside": False}
    return {
        "risk_adjusted": card.sharpe is not None,
        "calibration": card.brier_avg is not None,
        "magnitude": card.mz_slope is not None and card.mz_r2 is not None,
        "downside": card.max_drawdown is not None,
    }


def _check_scorecard(
    db: Session,
    portfolio_id: str | None,
    *,
    horizon: dict[str, Any] | None = None,
) -> Check:
    """Why does the UI say "insufficient data"?

    ``composite_score`` withholds the composite unless at least 3 of 4 axes are
    computable; the frontend renders that ``None`` as the literal string
    "insufficient data".

    Args:
        horizon: Pending-prediction maturity for this sleeve. When predictions
            are pending and none are overdue, the missing axes are waiting on
            the calendar and this is a *pending* stage, not a broken one.
    """
    if portfolio_id is None:
        return Check(
            key="scorecard",
            label="Scorecard",
            status=SKIPPED,
            detail="No champion sleeve yet.",
        )

    card = (
        db.query(AdvisorScorecard)
        .filter(AdvisorScorecard.portfolio_id == portfolio_id)
        .order_by(AdvisorScorecard.window_end.desc())
        .first()
    )
    snapshots = db.query(PaperSnapshot).filter(PaperSnapshot.portfolio_id == portfolio_id).count()
    axes = _axis_availability(card)
    computable = sum(1 for v in axes.values() if v)

    from app.decision.advisor.evolution import composite_score

    composite, detail = composite_score(card)

    missing_reasons: list[str] = []
    if not axes["risk_adjusted"] or not axes["downside"]:
        missing_reasons.append(
            f"NAV axes need ≥{_MIN_NAV_RETURNS} daily returns in the 90-day window "
            f"(sleeve has {snapshots} snapshot(s) in total)"
        )
    if not axes["calibration"]:
        missing_reasons.append("calibration needs ≥1 resolved prediction")
    if not axes["magnitude"]:
        missing_reasons.append(
            f"magnitude needs ≥{_MIN_MAGNITUDE_PAIRS} resolved predictions with an expected return"
        )

    horizon = horizon or {}
    # Pending rows that have not come due yet are the calendar doing its job.
    waiting = bool(horizon.get("pending")) and not horizon.get("overdue")
    data = {
        "axes_computable": axes,
        "axes_computable_count": computable,
        "axes_total": len(axes),
        "axes_required_for_composite": _MIN_COMPOSITE_AXES,
        "composite": composite,
        "composite_detail": detail,
        "n_predictions": card.n_predictions if card else 0,
        "n_resolved": card.n_resolved if card else 0,
        "snapshots": snapshots,
        "snapshots_required_for_sharpe_on_snapshot": _MIN_METRIC_SNAPSHOTS,
        "blocking_reasons": missing_reasons,
        "pending_predictions": horizon.get("pending", 0),
        "next_prediction_resolves_at": horizon.get("next_resolve_at"),
        "days_until_next_resolution": horizon.get("days_to_next"),
        "waiting_on_horizon": waiting,
    }

    if card is None:
        return Check(
            key="scorecard",
            label="Scorecard",
            status=WARN if waiting else FAIL,
            detail=(
                "No scorecard row has ever been computed for this sleeve."
                + (
                    f" {horizon['pending']} prediction(s) are pending; the first matures in "
                    f"{horizon['days_to_next']:.0f} day(s)."
                    if waiting and horizon.get("days_to_next") is not None
                    else ""
                )
            ),
            data=data,
            remedy="The scorecard is written by the discovery_resolution job once predictions mature.",
        )
    if composite is None:
        # F15: magnitude is required specifically, not just "any 3 of 4" —
        # a forecaster with no magnitude signal at all is exactly the case
        # this axis exists to catch, so say so explicitly when that's why.
        magnitude_clause = (
            " magnitude specifically is required and not yet computable."
            if not axes["magnitude"]
            else ""
        )
        detail_text = (
            f"Composite withheld — {computable} of {len(axes)} axes are computable and "
            f"{_MIN_COMPOSITE_AXES} are required (including magnitude)."
            f"{magnitude_clause} This is exactly what the UI shows as "
            '"insufficient data". ' + "; ".join(missing_reasons)
        )
        if waiting:
            eta = horizon.get("days_to_next")
            return Check(
                key="scorecard",
                label="Scorecard",
                status=WARN,
                detail=(
                    f"{detail_text} Not a fault: {horizon['pending']} prediction(s) are still "
                    "inside their 21-trading-day horizon"
                    + (f", the first maturing in {eta:.0f} day(s)." if eta is not None else ".")
                ),
                data=data,
                remedy=(
                    "Nothing to do but wait for the horizon; check the "
                    "discovery_resolution job is running so the rows resolve on time."
                ),
            )
        return Check(
            key="scorecard",
            label="Scorecard",
            status=FAIL,
            detail=detail_text,
            data=data,
            remedy="Needs resolved predictions, which need executed trades.",
        )
    return Check(
        key="scorecard",
        label="Scorecard",
        status=OK,
        detail=f"Composite {composite:.3f} from {computable}/4 axes ({card.n_resolved} resolved predictions).",
        data=data,
    )


def _check_learning(
    db: Session,
    user_id: str,
    ref: datetime,
    *,
    horizon: dict[str, Any] | None = None,
) -> Check:
    """Has the LLM ever received feedback from its own track record?

    Lessons are the entire learning mechanism: ``reflect_on_cycle`` only runs
    when a sleeve has resolved predictions, and ``get_active_lessons`` feeds
    them back into the next decision prompt. Zero lessons means the prompt's
    ``lessons`` array has always been empty and the model has learned nothing.
    """
    from app.decision.advisor.evolution import MIN_RESOLVED_DECISIONS

    strategies = db.query(AdvisorStrategy).filter(AdvisorStrategy.user_id == user_id).all()
    sleeve_ids = [s.portfolio_id for s in strategies if s.portfolio_id]

    lessons = db.query(StrategyLesson).filter(StrategyLesson.user_id == user_id).all()
    active = [row for row in lessons if row.active]
    newest = max(_aware_all([row.created_at for row in lessons]), default=None)

    resolved_by_sleeve: dict[str, int] = {}
    for sleeve_id in sleeve_ids:
        resolved_by_sleeve[sleeve_id] = (
            db.query(DiscoveryPrediction)
            .filter(
                DiscoveryPrediction.user_id == user_id,
                DiscoveryPrediction.portfolio_id == sleeve_id,
                DiscoveryPrediction.outcome_status == "resolved",
            )
            .count()
        )
    min_resolved = min(resolved_by_sleeve.values()) if resolved_by_sleeve else 0

    horizon = horizon or {}
    waiting = bool(horizon.get("pending")) and not horizon.get("overdue")
    data = {
        "lessons_total": len(lessons),
        "lessons_active": len(active),
        "newest_lesson_at": newest.isoformat() if newest else None,
        "days_since_newest_lesson": round((_age_hours(newest, ref=ref) or 0.0) / 24.0, 1)
        if newest
        else None,
        "resolved_predictions_by_sleeve": resolved_by_sleeve,
        "promotion_sample_required": MIN_RESOLVED_DECISIONS,
        "promotion_sample_shortfall": max(0, MIN_RESOLVED_DECISIONS - min_resolved),
        "active_lesson_texts": [row.lesson_text for row in active[:10]],
        "pending_predictions": horizon.get("pending", 0),
        "next_prediction_resolves_at": horizon.get("next_resolve_at"),
        "days_until_next_resolution": horizon.get("days_to_next"),
        "waiting_on_horizon": waiting,
    }

    if not lessons:
        base = (
            "No lesson has been written yet, so every decision prompt so far carried an empty "
            "'lessons' array — the model has not yet seen its own track record. Reflection "
            f"only fires for a sleeve with resolved predictions; the weakest sleeve has "
            f"{min_resolved} (evolution needs {MIN_RESOLVED_DECISIONS} on both sleeves before "
            "it will ever promote or retire a strategy)."
        )
        if waiting:
            eta = horizon.get("days_to_next")
            return Check(
                key="learning",
                label="Learning loop",
                status=WARN,
                detail=(
                    f"{base} Not a fault yet: {horizon['pending']} prediction(s) are inside "
                    "their horizon"
                    + (f", the first maturing in {eta:.0f} day(s)." if eta is not None else ".")
                ),
                data=data,
                remedy=(
                    "Nothing to fix — the first reflection can only run after the first "
                    "prediction resolves."
                ),
            )
        return Check(
            key="learning",
            label="Learning loop",
            status=FAIL,
            detail=(
                f"{base} No prediction is pending either, so nothing will ever resolve and "
                "the loop cannot learn."
            ),
            data=data,
            remedy="Restore trade flow so predictions resolve; reflection and evolution follow from that.",
        )
    if not active:
        return Check(
            key="learning",
            label="Learning loop",
            status=WARN,
            detail=f"{len(lessons)} lesson(s) exist but all have decayed below the activation rank.",
            data=data,
            remedy="Lessons decay by 0.85 per reflection; without fresh ones the memory empties.",
        )
    return Check(
        key="learning",
        label="Learning loop",
        status=OK if min_resolved >= MIN_RESOLVED_DECISIONS else WARN,
        detail=(
            f"{len(active)} active lesson(s) feed the decision prompt. "
            f"Weakest sleeve has {min_resolved}/{MIN_RESOLVED_DECISIONS} resolved predictions "
            "required before evolution can promote or retire."
        ),
        data=data,
    )


def _check_cash_headroom(db: Session, portfolio_id: str | None) -> Check:
    """Can the sleeve fund a trade — from cash *or* from selling something?

    The sleeve mirrors a near-fully-invested real book, so a low cash balance
    is the normal state, not a fault. Buys are financed by the sells the model
    chooses in the same cycle (the executor settles every sell before any buy).
    What actually blocks trading is having neither cash nor a sellable position
    large enough to clear the minimum ticket.
    """
    if portfolio_id is None:
        return Check(
            key="cash_headroom",
            label="Cash headroom",
            status=SKIPPED,
            detail="No champion sleeve yet.",
        )

    from app.decision.paper_portfolio import _current_cash_balance, get_summary

    portfolio = db.get(PaperPortfolio, portfolio_id)
    if portfolio is None:
        return Check(
            key="cash_headroom",
            label="Cash headroom",
            status=SKIPPED,
            detail="Sleeve not found.",
        )

    cash = _current_cash_balance(portfolio, db)
    try:
        total_value = Decimal(str(get_summary(db, portfolio_id).get("total_value") or 0))
    except Exception:
        total_value = cash

    from app.decision.advisor.cycle import DEFAULT_MAX_TURNOVER_PCT, DEFAULT_MIN_TICKET_PCT

    min_ticket = total_value * Decimal(str(DEFAULT_MIN_TICKET_PCT))
    turnover_budget = total_value * Decimal(str(DEFAULT_MAX_TURNOVER_PCT))
    # Anything invested can be sold to fund a buy in the same cycle.
    sellable = max(Decimal("0"), total_value - cash)
    data = {
        "cash_balance": round(float(cash), 2),
        "total_value": round(float(total_value), 2),
        "sellable_securities": round(float(sellable), 2),
        "min_ticket": round(float(min_ticket), 2),
        "turnover_budget": round(float(turnover_budget), 2),
        # A zero-value sleeve has a zero minimum ticket, which would otherwise
        # read as "affordable" when there is in fact nothing to trade.
        "can_fund_a_trade": bool(total_value > 0 and (cash >= min_ticket or sellable >= min_ticket)),
    }

    if total_value <= 0:
        return Check(
            key="cash_headroom",
            label="Trade funding",
            status=FAIL,
            detail=(
                "Sleeve total value is zero — there is neither cash to spend nor "
                "a position to sell, so no order can be funded."
            ),
            data=data,
            remedy="Re-seed the sleeve from the real book (POST /api/advisor/cycle/run seeds on first use).",
        )
    if cash < min_ticket:
        return Check(
            key="cash_headroom",
            label="Trade funding",
            status=OK,
            detail=(
                f"Cash is €{float(cash):,.2f}, so buys are financed by sells — "
                f"€{float(sellable):,.2f} of holdings are sellable, up to "
                f"€{float(turnover_budget):,.2f} per cycle. This is the expected state "
                "for a sleeve mirroring a fully-invested book."
            ),
            data=data,
        )
    return Check(
        key="cash_headroom",
        label="Trade funding",
        status=OK,
        detail=(
            f"Cash €{float(cash):,.2f} covers the €{float(min_ticket):,.2f} minimum ticket "
            f"outright; up to €{float(turnover_budget):,.2f} tradeable per cycle."
        ),
        data=data,
    )


_PROBE_SYSTEM = (
    "You are a JSON API. Respond with exactly one JSON object, no markdown, "
    'matching: {"decisions": [{"ticker": "SYM", "thesis": "one sentence", '
    '"action": "buy"|"sell"|"hold", "target_weight": 0.08, '
    '"confidence": 0.7}]}\n'
    "A negative `delta_to_target_eur` means the position must be reduced: "
    "answer `sell` with target_weight set to `suggested_weight`. A positive "
    "one means `buy`. Buys are funded only from cash plus the proceeds of the "
    "sells in the same response."
)
# A cash-starved book whose only correct answer includes a sell. The previous
# probe asked one ticker against an empty book with no funding constraint, so a
# green result was fully consistent with a decision stage that had never
# produced a trade — it exercised JSON encoding, not the decision.
_PROBE_USER = json.dumps({
    "allowed_tickers": ["ASML.AS", "OVERWEIGHT.AS"],
    "candidates": [
        {
            "ticker": "OVERWEIGHT.AS", "held": True, "held_eur": 9000.0,
            "current_weight": 0.9, "suggested_weight": 0.5,
            "delta_to_target_eur": -4000.0, "direction": "sell", "actionable": True,
        },
        {
            "ticker": "ASML.AS", "held": False, "held_eur": 0.0,
            "current_weight": 0.0, "suggested_weight": 0.4,
            "delta_to_target_eur": 4000.0, "direction": "buy", "actionable": True,
        },
    ],
    "sell_candidates": [
        {"ticker": "OVERWEIGHT.AS", "held_eur": 9000.0, "suggested_weight": 0.5,
         "excess_eur": 4000.0, "frees_cash_eur": 3990.0},
    ],
    "current_book_eur": {"OVERWEIGHT.AS": 9000.0},
    "cash_eur": 10.0,
    "portfolio_value_eur": 10000.0,
    "constraints": {"min_order_eur": 100.0},
    "lessons": [],
})


def _accepts_telemetry(call: Callable[..., Any]) -> bool:
    """Does *call* take a ``telemetry`` keyword (directly or via ``**kwargs``)?"""
    import inspect

    try:
        params = inspect.signature(call).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return False
    if "telemetry" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _llm_context_budget(
    base_url: str,
    prompt_tokens: int | None,
    max_tokens: int,
) -> dict[str, Any]:
    """Compare prompt + completion against the server's actual context window.

    llama-server runs with ``--no-context-shift``, so a request whose prompt plus
    completion budget exceeds ``--ctx-size`` has its generation cut instead of
    its history rolled — and under a grammar a cut completion is unparseable
    JSON. The window is only knowable by asking the server, so this is
    best-effort and stays silent when ``/props`` is unavailable.
    """
    info: dict[str, Any] = {"prompt_tokens": prompt_tokens, "max_tokens": max_tokens}
    # ``/props`` lives at the server root, not under the OpenAI-compatible /v1.
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        import httpx

        response = httpx.get(f"{root}/props", timeout=httpx.Timeout(10.0, connect=3.0))
        response.raise_for_status()
        props = response.json()
    except Exception as exc:  # pragma: no cover - depends on a live endpoint
        info["n_ctx"] = None
        info["note"] = f"could not read {root}/props: {exc}"
        return info

    settings = props.get("default_generation_settings") or {}
    n_ctx = settings.get("n_ctx") or props.get("n_ctx")
    info["n_ctx"] = n_ctx
    if isinstance(n_ctx, int) and n_ctx > 0 and prompt_tokens:
        needed = int(prompt_tokens) + int(max_tokens)
        info["worst_case_tokens"] = needed
        info["headroom_tokens"] = n_ctx - needed
        info["fits"] = needed <= n_ctx
    return info


def _check_llm(
    db: Session,
    *,
    probe: bool,
    llm_call: Callable[[Session, list[dict[str, str]]], str] | None = None,
) -> Check:
    """Does the LLM endpoint return schema-valid JSON for the decision contract?

    This is the stage that failed silently for weeks: Qwen3's hidden ``<think>``
    block consumed the completion budget before any content was emitted, so
    ``decide_trades`` got unparseable output and returned zero decisions.
    """
    from app.foundation.settings import resolve_llm_base_url, resolve_llm_model

    base_url = resolve_llm_base_url(db)
    model = resolve_llm_model(db)
    data: dict[str, Any] = {"base_url": base_url, "model": model, "probed": probe}

    if not probe:
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=SKIPPED,
            detail=f"Not probed (endpoint {base_url}, model {model}). Re-run with probe_llm=true.",
            data=data,
        )

    from app.decision.advisor.llm_decision import (
        DECISION_SAMPLING,
        build_decision_schema,
        completion_budget,
        parse_completion,
    )

    # The probe has to make the *same shape* of request the cycle makes.
    # Calling the endpoint bare — no json_schema, no sampling preset — tested
    # only whether the model can emit JSON, so this stage reported OK while
    # every grammar-constrained decision call was failing.
    probe_tickers = ["ASML.AS", "OVERWEIGHT.AS"]
    schema = build_decision_schema(probe_tickers)
    budget = completion_budget(len(probe_tickers))
    telemetry: dict[str, Any] = {}
    data.update({"request_shape": "json_schema + decision sampling preset", "max_tokens": budget})

    def _live_call(session: Session, messages: list[dict[str, str]]) -> str:
        from app.decision.llm_portfolio.review import _local_llm_sync

        return _local_llm_sync(
            session,
            messages,
            json_schema=schema,
            sampling=DECISION_SAMPLING,
            max_tokens=budget,
            telemetry=telemetry,
        )

    call = llm_call or _live_call
    # An injected fake may want to report what the endpoint would have said
    # about itself. Passing ``telemetry`` unconditionally would break the plain
    # two-argument fakes, so it is offered only to callables that accept it.
    accepts_telemetry = call is not _live_call and _accepts_telemetry(call)

    started = datetime.now(UTC)
    try:
        messages = [
            {"role": "system", "content": _PROBE_SYSTEM},
            {"role": "user", "content": _PROBE_USER},
        ]
        raw = (
            call(db, messages, telemetry=telemetry)  # type: ignore[call-arg]
            if accepts_telemetry
            else call(db, messages)
        )
    except Exception as exc:
        data["error"] = str(exc)
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=FAIL,
            detail=f"LLM call failed against {base_url}: {exc}",
            data=data,
            remedy="Check the llama.cpp service is up and the configured base URL/model are correct.",
        )

    elapsed = (datetime.now(UTC) - started).total_seconds()
    parsed, parsed_as = parse_completion(raw)
    has_think = "<think>" in raw or "</think>" in raw
    data.update(
        {
            "latency_s": round(elapsed, 2),
            "response_chars": len(raw),
            "parsed_json": parsed is not None,
            "parsed_as": parsed_as,
            "has_reasoning_leak": has_think,
            "response_preview": raw[:500],
            **telemetry,
        }
    )
    if llm_call is None:
        # Only meaningful against the real endpoint; an injected call has no
        # server to interrogate.
        data["context"] = _llm_context_budget(base_url, telemetry.get("prompt_tokens"), budget)

    if telemetry.get("truncated"):
        # A grammar cannot produce malformed JSON; a completion cut off at the
        # token limit can, and the two are indistinguishable in the error text.
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=FAIL,
            detail=(
                f"The completion hit the {budget}-token limit and stopped mid-object "
                "(finish_reason=length) on a two-ticker probe. A real cycle asks for a dozen, "
                "so its JSON is cut off too — which the cycle log records as "
                '"LLM response was not valid JSON".'
            ),
            data=data,
            remedy=(
                "Bound the completion: cap the thesis length in the decision schema so the "
                "one unbounded grammar field cannot run to the limit, and check the sleeve's "
                "universe size against max_tokens."
            ),
        )
    if telemetry.get("schema_dropped"):
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=FAIL,
            detail=(
                f"The server rejected the JSON schema ({telemetry.get('schema_reject_status')}) "
                "and the call fell back to unconstrained generation, so nothing enforces the "
                "decision contract."
            ),
            data=data,
            remedy=(
                "Check the llama-server build accepts response_format={'type':'json_object',"
                "'schema':…}; note it also fails open when a schema converts to a grammar it "
                "cannot parse, logging 'failed to parse grammar' and returning 200."
            ),
        )

    if has_think:
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=FAIL,
            detail=(
                "The response contains a <think> block — reasoning mode is still on. "
                "Thinking tokens consume the completion budget before any JSON is emitted, "
                "which is what starved the advisor decision step."
            ),
            data=data,
            remedy=(
                "llama-server ignores the OpenAI `reasoning_effort` field. Disable thinking with "
                "the `--reasoning-budget 0` server flag (or `--chat-template-kwargs "
                "'{\"enable_thinking\": false}'`) and restart quantfolio-llamacpp."
            ),
        )
    if parsed is None:
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=FAIL,
            detail=f"Endpoint responded in {elapsed:.1f}s but the output was not parseable JSON.",
            data=data,
            remedy="Constrain output with response_format/json_schema instead of prompt-only JSON.",
        )
    if not isinstance(parsed.get("decisions"), list):
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=WARN,
            detail="Response was valid JSON but did not match the decisions schema.",
            data=data,
            remedy="Constrain output with response_format/json_schema.",
        )

    # The probe book is 90% in one name against €10 of cash, so the only
    # coherent answer trims it. JSON validity alone says nothing about whether
    # the decision stage works.
    decisions = [d for d in parsed["decisions"] if isinstance(d, dict)]
    sells = [
        d for d in decisions
        if str(d.get("action", "")).lower().strip() == "sell"
    ]
    data["probe_actions"] = [
        {"ticker": d.get("ticker"), "action": d.get("action")} for d in decisions
    ]
    data["probe_proposed_sell"] = bool(sells)
    if not sells:
        return Check(
            key="llm",
            label="LLM decision endpoint",
            status=WARN,
            detail=(
                f"Returned schema-valid JSON in {elapsed:.1f}s, but proposed no sell on a "
                "book that is 90% one position with €10 of cash — the only way to fund a "
                "buy here is to trim. The decision stage answers, but not usefully."
            ),
            data=data,
            remedy=(
                "Check that the decision prompt still carries signed "
                "`delta_to_target_eur` and a populated `sell_candidates` block; a "
                "sell-blind prompt is what produced one trade in twenty days."
            ),
        )
    return Check(
        key="llm",
        label="LLM decision endpoint",
        status=OK,
        detail=(
            f"Returned schema-valid JSON in {elapsed:.1f}s and correctly proposed a sell "
            "to fund the buy on a cash-starved book."
        ),
        data=data,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_advisor_diagnostics(
    db: Session,
    user_id: str,
    *,
    probe_llm: bool = False,
    llm_call: Callable[[Session, list[dict[str, str]]], str] | None = None,
) -> dict[str, Any]:
    """Run every stage check and return a single health report.

    Args:
        db: Active session.
        user_id: Owner of the sleeves being inspected.
        probe_llm: When True, make a real call to the configured LLM endpoint
            using the same structured-JSON contract the advisor uses.
        llm_call: Injectable LLM callable for tests.

    Returns:
        ``{status, generated_at, summary, checks, blocking}`` where ``status``
        is the worst stage status and ``blocking`` lists the failing stages in
        pipeline order — the first entry is the thing to fix first.
    """
    ref = now_utc()
    holidays = _market_holidays(db)

    from app.decision.advisor.strategy import get_active_challenger, get_champion

    champion = get_champion(db, user_id)
    challenger = get_active_challenger(db, user_id)
    champion_portfolio_id = champion.portfolio_id if champion else None
    sleeve_ids = [
        s.portfolio_id
        for s in (champion, challenger)
        if s is not None and s.portfolio_id
    ]

    candidate_check = _check_candidate_feed(db, user_id, ref)
    candidate_symbols: list[str] = list(
        (candidate_check.data.get("latest_run") or {}).get("symbols") or []
    )
    holding_symbols: list[str] = []
    if champion_portfolio_id:
        holding_symbols = [
            h.ticker
            for h in db.query(PaperHolding)
            .filter(PaperHolding.portfolio_id == champion_portfolio_id)
            .all()
            if h.ticker
        ]
    symbols = list(dict.fromkeys([*candidate_symbols, *holding_symbols]))

    champion_mandate: str | None = None
    if champion_portfolio_id:
        champ_pf = db.get(PaperPortfolio, champion_portfolio_id)
        champion_mandate = champ_pf.mandate if champ_pf is not None else None

    upstream = [
        _check_scheduler(db, ref),
        _check_sleeves(db, user_id),
        candidate_check,
        _check_price_history(db, user_id, symbols),
        _check_llm(db, probe=probe_llm, llm_call=llm_call),
        _check_cycle_history(
            db, champion_portfolio_id, ref, holidays, mandate=champion_mandate
        ),
        _check_cash_headroom(db, champion_portfolio_id),
    ]
    # Everything below the decision stage is downstream of it. Naming the first
    # upstream failure lets those stages report themselves as consequences
    # instead of each claiming to be an independent blocker — the report showed
    # four blockers for one root cause, and the ordered "fix these in order"
    # list buried the only one worth acting on.
    first_break = next((c.label for c in upstream if c.status == FAIL), None)
    champion_horizon = _pending_horizon(
        db, user_id, [champion_portfolio_id] if champion_portfolio_id else [], ref
    )
    sleeve_horizon = _pending_horizon(db, user_id, sleeve_ids, ref)

    checks = [
        *upstream,
        _check_trade_activity(
            db,
            champion_portfolio_id,
            ref,
            upstream_broken=first_break,
            skip_reasons=_latest_skip_reasons(db, champion_portfolio_id, champion_mandate),
        ),
        _check_prediction_ledger(db, user_id, sleeve_ids, ref),
        _check_scorecard(db, champion_portfolio_id, horizon=champion_horizon),
        _check_learning(db, user_id, ref, horizon=sleeve_horizon),
    ]

    overall = OK
    for check in checks:
        if _SEVERITY[check.status] > _SEVERITY[overall]:
            overall = check.status

    blocking = [
        {"key": c.key, "label": c.label, "detail": c.detail, "remedy": c.remedy}
        for c in checks
        if c.status == FAIL
    ]
    # Stages held up by an unelapsed prediction horizon, listed separately so
    # "waiting" never reads as "broken".
    pending = [
        {"key": c.key, "label": c.label, "detail": c.detail, "remedy": c.remedy}
        for c in checks
        if c.status == WARN and bool(c.data.get("waiting_on_horizon"))
    ]

    counts = {level: sum(1 for c in checks if c.status == level) for level in (OK, WARN, FAIL, SKIPPED)}
    waiting_note = (
        f" {len(pending)} stage(s) are waiting on predictions that have not matured yet."
        if pending
        else ""
    )
    if blocking:
        summary = (
            f"The loop is broken at '{blocking[0]['label']}' "
            f"({counts[FAIL]} failing, {counts[WARN]} warning). "
            "Stages are listed in pipeline order — fix the first failure first."
            + waiting_note
        )
    elif counts[WARN]:
        summary = (
            f"The loop is running with {counts[WARN]} warning(s) and no blockers."
            + waiting_note
        )
    else:
        summary = "All stages healthy."

    return {
        "status": overall,
        "generated_at": ref.isoformat(),
        "user_id": user_id,
        "summary": summary,
        "counts": counts,
        "blocking": blocking,
        "pending": pending,
        "checks": [asdict(c) for c in checks],
    }
