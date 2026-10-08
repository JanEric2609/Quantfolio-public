"""Portfolio Advisor API endpoints."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.foundation.recommendation_execution import (
    accepted_and_executed,
    broker_links,
    record_acceptance,
    recommendation_isin,
)
from app.foundation.recommendation_execution import side as execution_side
from app.foundation.core.db import get_db
from app.foundation.models.entities import Portfolio, Recommendation, RecommendationDossier, RecommendationReview, User
from app.foundation.auth import current_user
from app.foundation.portfolio_service import main_portfolio
from app.decision.portfolio_advisor.pulse import run_pulse_check
from app.decision.portfolio_advisor.deep import run_deep_analysis, make_llm_func
from app.foundation.etf_lookthrough import portfolio_lookthrough
from app.lab.regime.macro_snapshot import get_or_refresh_regime
from app.decision.regime_advisor import get_regime_adjusted_weights, get_weights_summary, get_factor_ics, run_mwu_update, update_factor_ic_tracking

router = APIRouter(prefix="/api/portfolio/advisor", tags=["portfolio-advisor"])


@router.get("/analyze")
def analyze_portfolio(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    mode: str = Query("pulse", description="pulse or deep"),
) -> dict[str, Any]:
    """Run portfolio analysis — pulse (quick) or deep (detailed)."""
    portfolio = main_portfolio(db, user.id)
    # Reload with selectinload to get holdings
    portfolio = (
        db.query(Portfolio)
        .options(selectinload(Portfolio.holdings))
        .filter(Portfolio.id == portfolio.id)
        .first()
    )
    by_isin: dict[str, dict] = {}
    for h in portfolio.holdings if portfolio else []:
        isin = h.isin or h.ticker or h.name or ""
        if isin in by_isin:
            # merge: sum quantities, average price weighted by quantity
            existing = by_isin[isin]
            total_qty = existing["quantity"] + float(h.quantity)
            existing["avg_buy_price"] = (
                (existing["avg_buy_price"] * existing["quantity"])
                + (float(h.avg_buy_price or 0) * float(h.quantity))
            ) / total_qty if total_qty > 0 else 0
            existing["quantity"] = total_qty
            price = float(cp) if (cp := getattr(h, "current_price", None)) is not None else float(h.avg_buy_price or 0)
            existing["current_value"] = existing["quantity"] * price
        else:
            avg_price = float(h.avg_buy_price) if h.avg_buy_price else 0
            qty = float(h.quantity)
            price = float(cp) if (cp := getattr(h, "current_price", None)) is not None else avg_price
            by_isin[isin] = {
                "name": h.name,
                "ticker": h.ticker,
                "isin": h.isin,
                "asset_type": h.asset_type,
                "quantity": qty,
                "avg_buy_price": avg_price,
                "current_value": qty * price,
            }
    holdings = list(by_isin.values())

    regime = get_or_refresh_regime(db)
    regime_label = regime.get("label", "unknown")
    regime_weights = get_regime_adjusted_weights(db, regime_label)
    lookthrough = portfolio_lookthrough(db, user.id)

    if mode == "deep":
        result = run_deep_analysis(holdings, regime, llm_func=make_llm_func(db), regime_weights=regime_weights, lookthrough=lookthrough)
    else:
        result = run_pulse_check(holdings, regime, regime_weights, lookthrough)

    return {"mode": mode, "result": result, "regime": regime, "regime_weights": regime_weights}


# --- Advisor run management (stores runs as Recommendations with mode="portfolio_advisor") ---

ADVISOR_MODE = "portfolio_advisor"


def _store_advisor_run(db: Session, user_id: str, mode: str, result: dict, regime: dict) -> Recommendation:
    """Store an advisor analysis run as a Recommendation record."""
    run_id = str(uuid.uuid4())
    regime_label = regime.get("label", "unknown")
    regime_weights = get_regime_adjusted_weights(db, regime_label)
    payload = {
        "run_id": run_id,
        "mode": mode,
        "report_markdown": result.get("llm_analysis") if mode == "deep" else None,
        "regime_label": regime_label,
        "regime_weights": regime_weights,
        "pulse": result if mode == "pulse" else result.get("pulse"),
        "deep": result if mode == "deep" else None,
        "regime": regime,
        "checks": result.get("checks", []) if mode == "pulse" else [],
        "concentration": result.get("concentration") if mode == "pulse" else None,
        "drift_count": result.get("drift_count") if mode == "pulse" else None,
        "analysis": result.get("analysis") if mode == "deep" else None,
    }
    rec = Recommendation(
        id=run_id,
        user_id=user_id,
        ticker="EUNL.DE",  # benchmark proxy enables outcome evaluation
        horizon="mid",
        verdict="RUN" if mode == "pulse" else "DEEP_RUN",
        confidence=0.0,
        payload_json=json.dumps(payload),
        mode=ADVISOR_MODE,
        approval_state="complete",
        created_at=datetime.now(UTC),
    )
    db.add(rec)
    db.commit()
    return rec


@router.post("/run")
def trigger_advisor_run(
    background_tasks: BackgroundTasks,
    mode: str = Query("deep", description="pulse or deep"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Trigger an advisor analysis run. Returns immediately with run_id."""
    from app.foundation.core.db import SessionLocal

    def _run_advisor(user_id: str, mode: str) -> None:
        with SessionLocal() as run_db:
            u = run_db.query(User).filter(User.id == user_id).first()
            if not u:
                return
            try:
                portfolio = main_portfolio(run_db, user_id)
                # Reload with selectinload to get holdings
                portfolio = (
                    run_db.query(Portfolio)
                    .options(selectinload(Portfolio.holdings))
                    .filter(Portfolio.id == portfolio.id)
                    .first()
                )
                by_isin: dict[str, dict] = {}
                for h in portfolio.holdings if portfolio else []:
                    isin = h.isin or h.ticker or h.name or ""
                    if isin in by_isin:
                        # merge: sum quantities, average price weighted by quantity
                        existing = by_isin[isin]
                        total_qty = existing["quantity"] + float(h.quantity)
                        existing["avg_buy_price"] = (
                            (existing["avg_buy_price"] * existing["quantity"])
                            + (float(h.avg_buy_price or 0) * float(h.quantity))
                        ) / total_qty if total_qty > 0 else 0
                        existing["quantity"] = total_qty
                        price = float(cp) if (cp := getattr(h, "current_price", None)) is not None else float(h.avg_buy_price or 0)
                        existing["current_value"] = existing["quantity"] * price
                    else:
                        avg_price = float(h.avg_buy_price) if h.avg_buy_price else 0
                        qty = float(h.quantity)
                        price = float(cp) if (cp := getattr(h, "current_price", None)) is not None else avg_price
                        by_isin[isin] = {
                            "name": h.name,
                            "ticker": h.ticker,
                            "isin": h.isin,
                            "asset_type": h.asset_type,
                            "quantity": qty,
                            "avg_buy_price": avg_price,
                            "current_value": qty * price,
                        }
                holdings = list(by_isin.values())
                regime = get_or_refresh_regime(run_db)
                regime_label = regime.get("label", "unknown")
                regime_weights = get_regime_adjusted_weights(run_db, regime_label)
                lookthrough = portfolio_lookthrough(run_db, user_id)
                if mode == "deep":
                    result = run_deep_analysis(holdings, regime, llm_func=make_llm_func(run_db), regime_weights=regime_weights, lookthrough=lookthrough)
                else:
                    result = run_pulse_check(holdings, regime, regime_weights, lookthrough)
                _store_advisor_run(run_db, user_id, mode, result, regime)
            except Exception as exc:
                # Store failed run
                fail_rec = Recommendation(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    ticker=None,
                    horizon="mid",
                    verdict="FAILED",
                    confidence=0.0,
                    payload_json=json.dumps({"mode": mode, "error": str(exc)}),
                    mode=ADVISOR_MODE,
                    approval_state="failed",
                    created_at=datetime.now(UTC),
                )
                run_db.add(fail_rec)
                run_db.commit()

    background_tasks.add_task(_run_advisor, user.id, mode)
    return {"status": "triggered", "mode": mode}


@router.get("/runs")
def list_advisor_runs(
    limit: int = Query(20, description="Max runs to return"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """List advisor analysis runs."""
    rows = (
        db.query(Recommendation)
        .filter(
            Recommendation.user_id == user.id,
            Recommendation.mode == ADVISOR_MODE,
        )
        .order_by(Recommendation.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "runs": [
            {
                "id": r.id,
                "ticker": r.ticker,
                "verdict": r.verdict,
                "confidence": float(r.confidence) if r.confidence else 0,
                "mode": r.mode or ADVISOR_MODE,
                "approval_state": r.approval_state or "complete",
                "created_at": r.created_at.isoformat() if r.created_at else "",
                "payload_json": r.payload_json,
            }
            for r in rows
        ]
    }


@router.get("/runs/{run_id}")
def get_advisor_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get advisor run detail including parsed payload and sub-recommendations."""
    run = (
        db.query(Recommendation)
        .filter(
            Recommendation.id == run_id,
            Recommendation.user_id == user.id,
            Recommendation.mode == ADVISOR_MODE,
        )
        .first()
    )
    if not run:
        raise HTTPException(404, "Run not found")

    payload = json.loads(run.payload_json or "{}")

    return {
        "id": run.id,
        "ticker": run.ticker,
        "verdict": run.verdict,
        "confidence": float(run.confidence) if run.confidence else 0,
        "mode": run.mode or ADVISOR_MODE,
        "approval_state": run.approval_state or "complete",
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "payload_json": run.payload_json,
        "payload": payload,
        "recommendations": [],
    }


# Decision states that still wait for the owner: a discovery candidate that
# passed the track-record gate, or a generated v2 recommendation awaiting review.
# "draft" (auto-withheld as unproven) is deliberately absent: a withheld idea is
# visible in Discover but is never put in front of the owner as a decision.
PENDING_STATES = ("approved_candidate", "needs_review")
# A snoozed recommendation comes back after this long; Snooze means "later", not "no".
SNOOZE_DAYS = 7
PENDING_MAX_AGE_DAYS = 30
_SUMMARY_CHARS = 400


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _evidence_proven(payload: dict[str, Any]) -> bool:
    """True when the row carries a proven track record (the evidence test).

    Discover stores ``track_record_gate`` in its payload; any other generator
    counts only if it carries the same gate or an explicit ``evidence_proven``
    flag. Everything else is research, not a decision. Old Discover rows said
    BUY under a score-based rule, so the verdict column cannot be trusted.
    """
    gate = payload.get("track_record_gate")
    if isinstance(gate, dict) and gate.get("status") == "proven":
        return True
    return payload.get("evidence_proven") is True


def _normalised_confidence(value: Any) -> float:
    confidence = float(value) if value is not None else 0.0
    # Discover stores a 0-1 conviction, the v2 generator a 0-100 score.
    return confidence / 100 if confidence > 1 else confidence


@router.get("/pending")
def list_pending_recommendations(
    limit: int = Query(20, ge=1, le=100, description="Max items to return"),
    max_age_days: int = Query(PENDING_MAX_AGE_DAYS, ge=1, le=365, description="Ignore older recommendations"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Recommendations waiting for Accept / Reject / Snooze, newest first.

    Pending means approval state ``approved_candidate`` or ``needs_review``, not
    expired, created within ``max_age_days``; plus snoozed ones whose snooze is
    older than ``SNOOZE_DAYS``. ``total`` counts every pending item (for badges),
    ``items`` is capped at ``limit``. ``research_count`` is how many tickers were
    held back because their evidence is not proven yet (research, not decisions). Only the caller's own rows are returned.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(days=max_age_days)
    live = or_(Recommendation.recommendation_expiry.is_(None), Recommendation.recommendation_expiry > now)

    rows = (
        db.query(
            Recommendation.id,
            Recommendation.ticker,
            Recommendation.created_at,
            Recommendation.approval_state,
            Recommendation.payload_json,
        )
        .filter(
            Recommendation.user_id == user.id,
            Recommendation.created_at >= window_start,
            Recommendation.approval_state.in_((*PENDING_STATES, "snoozed", "draft")),
            live,
        )
        .all()
    )

    snoozed_ids = [r.id for r in rows if r.approval_state == "snoozed"]
    resurfaced: set[str] = set()
    if snoozed_ids:
        snooze_cutoff = now - timedelta(days=SNOOZE_DAYS)
        last_snooze = (
            db.query(RecommendationReview.recommendation_id, func.max(RecommendationReview.created_at))
            .filter(
                RecommendationReview.recommendation_id.in_(snoozed_ids),
                RecommendationReview.user_id == user.id,
                RecommendationReview.to_state == "snoozed",
            )
            .group_by(RecommendationReview.recommendation_id)
            .all()
        )
        for rec_id, snoozed_at in last_snooze:
            if snoozed_at is None:
                continue
            if snoozed_at.tzinfo is None:
                snoozed_at = snoozed_at.replace(tzinfo=UTC)
            if snoozed_at <= snooze_cutoff:
                resurfaced.add(rec_id)

    # Drafts (new Discover rows without proven evidence) are never shown, but
    # they are the ticker's newest word, so they take part in the dedupe.
    candidates = [
        r for r in rows if r.approval_state in (*PENDING_STATES, "draft") or r.id in resurfaced
    ]
    # Inbox = gate + dedupe: only proven evidence is a decision (a snoozed one
    # was shown before, so it comes back regardless), and one card per ticker,
    # newest row first. The rest is research that Discover still lists.
    # The newest row of a ticker decides: an older proven row does not
    # outlive a newer run that no longer finds the evidence.
    candidates.sort(key=lambda r: r.created_at, reverse=True)
    pending = []
    decision_tickers: set[str] = set()
    seen_tickers: set[str] = set()
    for r in candidates:
        key = r.ticker or r.id
        if key in seen_tickers:
            continue
        seen_tickers.add(key)
        if r.approval_state == "draft" or (
            r.id not in resurfaced and not _evidence_proven(_loads(r.payload_json))
        ):
            continue
        decision_tickers.add(key)
        pending.append(r)
    research_tickers = {
        r.ticker
        for r in rows
        if r.approval_state in (*PENDING_STATES, "draft")
        and r.ticker not in decision_tickers
        and not _evidence_proven(_loads(r.payload_json))
    }
    page_ids = [r.id for r in pending[:limit]]

    by_id = {}
    if page_ids:
        by_id = {
            rec.id: rec
            for rec in db.query(Recommendation).filter(Recommendation.id.in_(page_ids), Recommendation.user_id == user.id)
        }
    payloads = {rec_id: _loads(rec.payload_json) for rec_id, rec in by_id.items()}

    dossier_ids = [p["dossier_id"] for p in payloads.values() if isinstance(p.get("dossier_id"), str)]
    dossier_thesis: dict[str, str] = {}
    if dossier_ids:
        for dossier in db.query(RecommendationDossier).filter(RecommendationDossier.id.in_(dossier_ids)):
            if dossier.user_id not in (None, user.id):
                continue
            thesis = _loads(dossier.dossier_json).get("thesis")
            if isinstance(thesis, str):
                dossier_thesis[dossier.id] = thesis

    items: list[dict[str, Any]] = []
    for rec_id in page_ids:
        rec = by_id.get(rec_id)
        if rec is None:
            continue
        payload = payloads[rec_id]
        raw_candidate = payload.get("candidate")
        candidate: dict[str, Any] = raw_candidate if isinstance(raw_candidate, dict) else {}
        summary = payload.get("thesis") or dossier_thesis.get(str(payload.get("dossier_id"))) or ""
        items.append(
            {
                "id": rec.id,
                "ticker": rec.ticker,
                "name": candidate.get("name") or payload.get("name"),
                "verdict": rec.verdict,
                "confidence": _normalised_confidence(rec.confidence),
                "horizon": rec.horizon,
                "mode": rec.mode,
                "approval_state": rec.approval_state,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "expires_at": rec.recommendation_expiry.isoformat() if rec.recommendation_expiry else None,
                "summary": str(summary)[:_SUMMARY_CHARS],
                "resurfaced": rec.id in resurfaced,
                "isin": recommendation_isin(rec),
                "side": execution_side(rec),
                "links": broker_links(recommendation_isin(rec)),
            }
        )
    return {"items": items, "total": len(pending), "research_count": len(research_tickers)}


@router.post("/feedback/{rec_id}")
def submit_advisor_feedback(
    rec_id: str,
    action: str = Query(..., description="accepted, rejected, or snoozed"),
    notes: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Submit feedback on an advisor recommendation or run."""
    rec = db.query(Recommendation).filter(
        Recommendation.id == rec_id,
        Recommendation.user_id == user.id,
    ).first()
    if not rec:
        raise HTTPException(404, "Recommendation not found")
    if action not in ("accepted", "rejected", "snoozed"):
        raise HTTPException(400, "action must be accepted, rejected, or snoozed")

    review = RecommendationReview(
        id=str(uuid.uuid4()),
        recommendation_id=rec.id,
        user_id=user.id,
        from_state=rec.approval_state,
        to_state=action,
        notes=notes,
    )
    db.add(review)
    rec.approval_state = action
    execution = None
    if action == "accepted":
        # The position at each broker now: the next sync compares against it.
        execution = record_acceptance(db, rec)
    db.commit()
    return {
        "ok": True,
        "new_state": action,
        "links": broker_links(recommendation_isin(rec)) if action == "accepted" else [],
        "execution": execution,
    }


@router.get("/accepted")
def list_accepted_recommendations(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Accepted recommendations still waiting for the trade, and those a broker sync has found done."""
    return accepted_and_executed(db, user.id)


# --- Closed-Loop Refinement (Phase 4) endpoints ---
#
# Note: this router used to also expose GET /verification/summary and
# GET /verification/outcomes (RecommendationOutcome-backed hit-rate/IR
# metrics). Removed 2026-08-27 — confirmed zero frontend consumers, and the
# name collided confusingly with the real, wired /api/verification/summary
# endpoint (api/verification.py). RecommendationOutcome itself is not dead:
# it still feeds regime_advisor's weekly MWU reweighting via
# run_outcome_evaluation/POST /evaluate below.


@router.post("/evaluate")
def run_evaluation(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Run outcome evaluation, MWU update, and factor IC tracking."""
    from app.foundation.recommendation_outcomes import run_outcome_evaluation

    evaluated = run_outcome_evaluation(db)
    mwu_updates = run_mwu_update(db)
    ic_updates = update_factor_ic_tracking(db)

    return {
        "outcomes_evaluated": evaluated,
        "mwu_weights_updated": mwu_updates,
        "factor_ics_updated": ic_updates,
    }


@router.get("/regime/weights")
def list_regime_weights(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get all regime recommendation weights."""
    return {"weights": get_weights_summary(db)}


@router.post("/regime/weights/refresh")
def refresh_regime_weights(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Force MWU update across all evaluated outcomes."""
    mwu_updates = run_mwu_update(db)
    return {"mwu_weights_updated": mwu_updates}


@router.get("/regime/factor-ics")
def list_factor_ics(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get all factor IC tracking records."""
    return {"factor_ics": get_factor_ics(db)}


@router.post("/regime/factor-ics/refresh")
def refresh_factor_ics(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Force factor IC update across all outcomes."""
    ic_updates = update_factor_ic_tracking(db)
    return {"factor_ics_updated": ic_updates}
