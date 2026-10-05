import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.interface.api.safe_endpoint import safe_endpoint
from app.foundation.core.db import get_db
from app.foundation.models.entities import LlmAdviceCard, LlmPortfolioDecision, PaperPortfolio, PaperSnapshot, User
from app.foundation.models.entities.competition import CompetitionDecision
from app.foundation.auth import current_user
from app.foundation.jobs import get_job_status, submit_job
from app.decision.llm_portfolio.divergence import compute_divergence as _divergence_service
from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm-portfolio", tags=["llm-portfolio"])


@router.post("/create-mandate")
@safe_endpoint
def create_mandate_portfolio(
    body: dict[str, Any],
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create (or return existing) paper portfolio for a given mandate.

    Call this when the user has no mandate portfolios yet. Creates one by
    mirroring real DKB holdings so the LLM review system can operate.
    """
    mandate = body.get("mandate", "A")
    if mandate not in ("A", "B"):
        raise HTTPException(status_code=422, detail="mandate must be 'A' or 'B'")

    portfolio = ensure_mandate_portfolio(db, user.id, mandate)
    return {
        "id": portfolio.id,
        "name": portfolio.name,
        "mandate": portfolio.mandate,
        "managed_by": portfolio.managed_by,
    }


@router.get("/")
@safe_endpoint
def list_llm_portfolios(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List all paper portfolios with mandate field and latest snapshot summary."""
    portfolios = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.user_id == user.id)
        .filter(PaperPortfolio.mandate.in_(["A", "B"]))
        .all()
    )
    result = []
    for p in portfolios:
        latest_snapshot = (
            db.query(PaperSnapshot)
            .filter(PaperSnapshot.portfolio_id == p.id)
            .order_by(PaperSnapshot.date.desc())
            .first()
        )
        holdings_count = len(p.holdings) if p.holdings else 0
        result.append({
            "id": p.id,
            "name": p.name,
            "mandate": p.mandate,
            "managed_by": p.managed_by,
            "total_value": str(latest_snapshot.total_value) if latest_snapshot else "0",
            "holdings_count": holdings_count,
            "cash_balance": str(latest_snapshot.cash_balance) if latest_snapshot else "0",
        })
    return result


@router.get("/{portfolio_id}/journal")
@safe_endpoint
def get_journal(
    portfolio_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List journal entries for this portfolio, ordered by review_date DESC.

    Merges LlmPortfolioDecision rows (from "Trigger Review") with
    CompetitionDecision rows (from competition rounds) into a single
    chronological feed.  Each item includes a ``source`` field:
    ``"review"`` or ``"competition"``.
    """
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.id == portfolio_id)
        .filter(PaperPortfolio.user_id == user.id)
        .first()
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # ── 1. Review decisions (existing shape) ──────────────────────────────
    review_decisions = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.portfolio_id == portfolio_id)
        .all()
    )
    entries: list[dict[str, Any]] = []
    for d in review_decisions:
        ts = d.review_date or d.created_at
        entries.append(
            {
                "id": d.id,
                "review_date": ts.isoformat() if ts else None,
                "mandate": d.mandate,
                "decision_json": json.loads(d.decision_json) if d.decision_json else {},
                "reflection_json": json.loads(d.reflection_json) if d.reflection_json else None,
                "context_token_estimate": d.context_token_estimate,
                "status": d.status,
                "verdict": d.verdict,
                "horizon_weeks": d.horizon_weeks,
                "scored_at": d.scored_at.isoformat() if d.scored_at else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "source": "review",
                "estimate": True,
                "not_financial_advice": True,
            }
        )

    # ── 2. Competition decisions ──────────────────────────────────────────
    comp_decisions = (
        db.query(CompetitionDecision)
        .filter(CompetitionDecision.portfolio_id == portfolio_id)
        .all()
    )
    for cd in comp_decisions:
        council_result = json.loads(cd.council_result_json) if cd.council_result_json else {}
        executed_trades = json.loads(cd.executed_trades_json) if cd.executed_trades_json else []
        score = json.loads(cd.score_json) if cd.score_json else {}

        # council_result is a CouncilResult dump: {portfolio_id, analysis, risk,
        # macro, decision, errors}. The real decision lives in `decision`
        # (trade_proposals[] + strategy_narrative), NOT top-level action/ticker/
        # rationale keys — reading those made every competition entry read
        # "hold"/null even when trades executed. Surface council errors too so a
        # failed LLM round is not silently shown as a genuine hold.
        decision = council_result.get("decision") or {}
        proposals = decision.get("trade_proposals") or []
        council_errors = council_result.get("errors") or []
        if proposals:
            first = proposals[0]
            action = first.get("action", "hold")
            ticker = first.get("asset_id")
        else:
            action = "hold"
            ticker = None
        decision_payload: dict[str, Any] = {
            "action": action,
            "ticker": ticker,
            "thesis": decision.get("strategy_narrative") or None,
            "proposals": proposals,
            "confidence": decision.get("confidence"),
            "errors": council_errors,
            "trades": executed_trades,
            "score": score,
            "round_number": cd.round_number,
            "winner": cd.winner,
        }

        ts = cd.created_at
        entries.append(
            {
                "id": cd.id,
                "review_date": ts.isoformat() if ts else None,
                "mandate": portfolio.mandate,
                "decision_json": decision_payload,
                "reflection_json": None,
                "context_token_estimate": None,
                "status": "winner" if cd.winner else "completed",
                "created_at": ts.isoformat() if ts else None,
                "source": "competition",
                "estimate": True,
                "not_financial_advice": True,
            }
        )

    # ── 3. Sort merged feed by review_date DESC ───────────────────────────
    def _sort_key(e: dict[str, Any]) -> datetime:
        raw = e.get("review_date")
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    entries.sort(key=_sort_key, reverse=True)
    return entries


@router.get("/{portfolio_id}/accuracy")
@safe_endpoint
def get_accuracy(
    portfolio_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Outcome-scoring accuracy summary for a mandate portfolio.

    Aggregates the ``verdict`` column set by the weekly scoring job into a
    hit-rate and verdict counts. ``hit_rate`` is over *scored* decisions only
    (hits / (hits + misses + partials)); ``unresolvable`` rows are excluded from
    the denominator so missing price data never inflates or deflates the score.
    """
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.id == portfolio_id)
        .filter(PaperPortfolio.user_id == user.id)
        .first()
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    scored = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.portfolio_id == portfolio_id)
        .filter(LlmPortfolioDecision.verdict.isnot(None))
        .all()
    )
    counts = {"hit": 0, "miss": 0, "partial": 0, "unresolvable": 0}
    for d in scored:
        if d.verdict is None:
            continue
        counts[d.verdict] = counts.get(d.verdict, 0) + 1

    resolved = counts["hit"] + counts["miss"] + counts["partial"]
    hit_rate = (counts["hit"] / resolved) if resolved else None

    # Awaiting scoring: completed decisions with a horizon but no verdict yet.
    pending = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.portfolio_id == portfolio_id)
        .filter(LlmPortfolioDecision.status == "completed")
        .filter(LlmPortfolioDecision.verdict.is_(None))
        .filter(LlmPortfolioDecision.horizon_weeks.isnot(None))
        .count()
    )

    return {
        "portfolio_id": portfolio_id,
        "mandate": portfolio.mandate,
        "counts": counts,
        "resolved": resolved,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "pending_scoring": pending,
        "estimate": True,
        "not_financial_advice": True,
    }


@router.get("/{portfolio_id}/divergence")
@safe_endpoint
def get_divergence(
    portfolio_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return paper vs real diff with advice cards."""
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.id == portfolio_id)
        .filter(PaperPortfolio.user_id == user.id)
        .first()
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    raw = _divergence_service(db, user.id, portfolio_id)

    # Query advice cards for this portfolio
    advice_cards = (
        db.query(LlmAdviceCard)
        .filter(LlmAdviceCard.portfolio_id == portfolio_id)
        .all()
    )

    return {
        "paper_vs_real": {
            "missing_in_real": raw.get("missing_in_real", []),
            "missing_in_paper": raw.get("missing_in_paper", []),
            "weight_diffs": raw.get("weight_diffs", []),
        },
        "performance_delta": raw.get("perf_delta", 0.0),
        "advice_cards": [
            {
                "id": c.id,
                "advice_text": c.advice_text,
                "performance_delta": float(c.performance_delta) if c.performance_delta is not None else 0.0,
                "status": c.status,
            }
            for c in advice_cards
        ],
    }


@router.post("/{portfolio_id}/reseed")
@safe_endpoint
def reseed_portfolio(
    portfolio_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Archive the current run and start again from the synced book (DKB, Scalable) at market value.

    Used to append the synced positions to whatever the sleeve held; it now
    goes through the same reset as POST /api/paper-portfolio/{id}/reset.
    """
    from app.decision.paper_portfolio import reset_portfolio

    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.id == portfolio_id)
        .filter(PaperPortfolio.user_id == user.id)
        .first()
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    result = reset_portfolio(db, portfolio.id, reason="reseed from the synced book")
    return {"id": portfolio.id, "holdings_count": result["holdings_count"], "archive_id": result["archive_id"]}


@router.patch("/advice-cards/{card_id}")
@safe_endpoint
def update_advice_card(
    card_id: str,
    body: dict[str, Any],
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update LlmAdviceCard status (acknowledged or dismissed)."""
    card = (
        db.query(LlmAdviceCard)
        .filter(LlmAdviceCard.id == card_id)
        .filter(LlmAdviceCard.user_id == user.id)
        .first()
    )
    if not card:
        raise HTTPException(status_code=404, detail="Advice card not found")

    status = body.get("status")
    if status not in ("acknowledged", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be 'acknowledged' or 'dismissed'")

    card.status = status
    db.commit()
    return {"id": card.id, "status": card.status}


@router.patch("/{portfolio_id}")
@safe_endpoint
def patch_portfolio(
    portfolio_id: str,
    body: dict[str, Any],
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update portfolio fields (e.g. set as active mandate)."""
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.id == portfolio_id)
        .filter(PaperPortfolio.user_id == user.id)
        .first()
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    if "active" in body:
        if not isinstance(body["active"], bool):
            raise HTTPException(status_code=422, detail="'active' must be a boolean")
        # Deactivate all other portfolios for this user, then activate this one
        db.query(PaperPortfolio).filter(PaperPortfolio.user_id == user.id).update(
            {"managed_by": "manual"}
        )
        # Must be "llm" (not "ai"): the CASH_SUFFICIENCY gate and the graduation
        # evaluator both filter on managed_by == "llm". Writing "ai" here left an
        # activated portfolio failing every buy gate as "cash_available: 0".
        portfolio.managed_by = "llm" if body["active"] else "manual"
    if "name" in body:
        portfolio.name = body["name"]
    if "mandate" in body:
        portfolio.mandate = body["mandate"]

    db.commit()
    return {"id": portfolio.id, "mandate": portfolio.mandate, "managed_by": portfolio.managed_by}


@router.post("/{portfolio_id}/review")
@safe_endpoint
def trigger_review(
    portfolio_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Submit an async mandate review job."""
    portfolio = (
        db.query(PaperPortfolio)
        .filter(PaperPortfolio.id == portfolio_id)
        .filter(PaperPortfolio.user_id == user.id)
        .first()
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    mandate = portfolio.mandate or "A"

    try:
        from app.decision.llm_portfolio.review import run_mandate_review
    except ImportError:
        raise HTTPException(status_code=503, detail="LLM portfolio review service unavailable: import failed")

    def _review_in_background(user_id: str, mandate_val: str) -> None:
        from app.foundation.core.db import SessionLocal
        db = SessionLocal()
        try:
            run_mandate_review(db, user_id, mandate_val)
        finally:
            db.close()

    job_id = submit_job(_review_in_background, args=(user.id, mandate))
    return {"job_id": job_id, "status": "scheduled"}


@router.get("/review/{job_id}")
@safe_endpoint
def review_job_status(
    job_id: str,
    _user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get the status of a submitted review job.

    Note: The endpoint does not check job ownership because APScheduler
    job IDs (UUID4, non-enumerable) are separate from the JobRun table.
    Only returns scheduled/not_found status — no user or portfolio data.
    """
    return get_job_status(job_id)
