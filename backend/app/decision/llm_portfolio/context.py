"""Prompt assembly for LLM portfolio review with bounded token budget."""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.live_positions import live_prices_by_isin
from app.foundation.models.entities import (
    DiscoverCandidate,
    LlmPortfolioDecision,
    PaperHolding,
    PaperPortfolio,
    PaperSnapshot,
    PortfolioSnapshot,
)
from app.foundation.portfolio.name_resolver import resolve_position_name
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

_MAX_CHARS = 28000
_CHAR_PER_TOKEN_ESTIMATE = 4


def assemble_review_context(
    db: Session,
    portfolio_id: str,
    mandate: str,
    max_chars: int = _MAX_CHARS,
) -> dict[str, Any]:
    """Build review context dict with messages, token estimate, and pre-computed blocks.

    Args:
        db: Database session.
        portfolio_id: PaperPortfolio UUID.
        mandate: Mandate identifier (e.g. "A" or "B").
        max_chars: Hard character budget (default 28_000).

    Returns:
        Dict with keys: messages, token_estimate, blocks.
    """
    blocks: dict[str, Any] = {}

    # 0. Deterministic assessment facts (highest priority). Computed in Python so
    # the LLM narrates rather than invents them. Never let a fact error abort the
    # whole review — fall back to an empty block.
    try:
        from app.decision.llm_portfolio.assessment import build_assessment

        portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.id == portfolio_id).one_or_none()
        if portfolio is not None:
            mandate_config = json.loads(portfolio.mandate_config_json or "{}")
            blocks["assessment"] = build_assessment(db, portfolio, mandate_config)
        else:
            blocks["assessment"] = {"error": "portfolio not found"}
    except Exception as exc:
        logger.warning("Assessment block failed: %s", exc)
        blocks["assessment"] = {"error": str(exc)}

    # 1. Portfolio positions
    blocks["positions"] = _build_positions_block(db, portfolio_id)

    # 2. P&L summary: last 3 PaperSnapshot rows
    blocks["pnl_summary"] = _build_pnl_block(db, portfolio_id)

    # 3. Performance vs benchmark since last review
    blocks["perf_vs_benchmark"] = _build_perf_block(db, portfolio_id)

    # 4. Last N journal entries (N=5)
    blocks["journal"] = _build_journal_block(db, portfolio_id)

    # 5. Divergence summary
    try:
        from app.decision.llm_portfolio.divergence import compute_divergence

        portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.id == portfolio_id).one_or_none()
        user_id = portfolio.user_id if portfolio else None
        if user_id:
            # SAVEPOINT: a SQL error inside compute_divergence must not abort
            # the outer Postgres transaction for the rest of the review.
            with db.begin_nested():
                blocks["divergence"] = compute_divergence(db, user_id, portfolio_id)
        else:
            blocks["divergence"] = {"error": "portfolio not found"}
    except Exception as exc:
        logger.warning("Divergence block failed: %s", exc)
        blocks["divergence"] = {"error": str(exc)}

    # 6. Investor profile
    blocks["investor_profile"] = _build_profile_block(db, portfolio_id)

    # 7. Regime label
    blocks["regime"] = _build_regime_block(db, portfolio_id)

    # 8. Latest Discover shortlist
    blocks["discover_shortlist"] = _build_discover_block(db)

    # Assemble text with priority truncation
    assembled = _assemble_text(blocks, max_chars)
    token_estimate = len(assembled) // _CHAR_PER_TOKEN_ESTIMATE

    system_prompt = _build_system_prompt(mandate)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": assembled},
    ]

    return {
        "messages": messages,
        "token_estimate": token_estimate,
        "blocks": blocks,
    }


def _build_positions_block(db: Session, portfolio_id: str) -> list[dict[str, Any]]:
    """List current paper holdings enriched with the broker's current_price where available."""
    holdings = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == portfolio_id)
        .all()
    )

    # Map ISIN -> current_price from the synced positions at every broker
    portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.id == portfolio_id).one_or_none()
    price_map: dict[str, Decimal] = live_prices_by_isin(db, portfolio.user_id) if portfolio else {}

    result = []
    for h in holdings:
        result.append({
            "isin": h.isin,
            "ticker": h.ticker,
            "name": resolve_position_name(
                {"name": h.name, "ticker": h.ticker, "isin": h.isin}, db
            ),
            "quantity": str(h.quantity),
            "avg_buy_price": str(h.avg_buy_price),
            "current_price": str(price_map.get(h.isin or "")) if price_map.get(h.isin or "") is not None else None,
        })
    return result


def _build_pnl_block(db: Session, portfolio_id: str) -> list[dict[str, Any]]:
    """Last 3 PaperSnapshot rows."""
    rows = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id)
        .order_by(PaperSnapshot.date.desc())
        .limit(3)
        .all()
    )
    return [
        {
            "date": str(r.date),
            "total_value": str(r.total_value),
            "cash_balance": str(r.cash_balance),
            "securities_value": str(r.securities_value),
            "total_return_pct": str(r.total_return_pct),
        }
        for r in rows
    ]


def _build_perf_block(db: Session, portfolio_id: str) -> dict[str, Any]:
    """Compare PaperSnapshot total_return_pct vs PortfolioSnapshot over matching dates."""
    paper_rows = (
        db.query(PaperSnapshot)
        .filter(PaperSnapshot.portfolio_id == portfolio_id)
        .order_by(PaperSnapshot.date.desc())
        .limit(3)
        .all()
    )
    if not paper_rows:
        return {"note": "No paper snapshots available"}

    portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.id == portfolio_id).one_or_none()
    if not portfolio:
        return {"note": "Portfolio not found"}

    # Find real portfolio snapshots for same dates
    real_snapshots = (
        db.query(PortfolioSnapshot)
        .filter(
            PortfolioSnapshot.user_id == portfolio.user_id,
            PortfolioSnapshot.source == "computed",
            PortfolioSnapshot.date.in_([r.date for r in paper_rows]),
        )
        .all()
    )
    real_by_date = {r.date: r for r in real_snapshots}

    comparisons = []
    for p in paper_rows:
        real = real_by_date.get(p.date)
        real_return_pct = None
        if real:
            real_return_pct = getattr(real, "total_return_pct", None)
            if real_return_pct is None and real.payload_json:
                try:
                    payload = json.loads(real.payload_json)
                    real_return_pct = payload.get("total_return_pct")
                except Exception:
                    pass
        comparisons.append({
            "date": str(p.date),
            "paper_return_pct": str(p.total_return_pct),
            "real_return_pct": str(real_return_pct) if real_return_pct is not None else None,
            "delta_pct": str(float(p.total_return_pct) - float(real_return_pct)) if real_return_pct is not None else None,
        })
    return {"comparisons": comparisons}


def _build_journal_block(db: Session, portfolio_id: str) -> list[dict[str, Any]]:
    """Last 5 completed LlmPortfolioDecision rows for this portfolio.

    Fallback rows (llm_unavailable / fallback_hold / invalid_action /
    review_failed) are excluded — they are outage/parse-failure artifacts,
    not decisions, and feeding them back would teach the LLM that it "chose"
    to hold.
    """
    rows = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.portfolio_id == portfolio_id)
        .filter(LlmPortfolioDecision.status == "completed")
        .order_by(LlmPortfolioDecision.review_date.desc())
        .limit(5)
        .all()
    )
    result = []
    for r in rows:
        try:
            decision = json.loads(r.decision_json or "{}")
        except json.JSONDecodeError:
            decision = {}
        try:
            reflection = json.loads(r.reflection_json or "{}") if r.reflection_json else {}
        except json.JSONDecodeError:
            reflection = {}
        result.append({
            "review_date": r.review_date.isoformat() if r.review_date else None,
            "mandate": r.mandate,
            "status": r.status,
            "decision_summary": decision,
            "reflection": reflection,
        })
    return result


def _build_profile_block(db: Session, portfolio_id: str) -> dict[str, Any]:
    """Read investor profile from public settings."""
    portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.id == portfolio_id).one_or_none()
    if not portfolio:
        return {}
    settings = get_public_settings(db)
    return {
        "discover_investor_horizon": settings.get("discover_investor_horizon"),
        "discover_investor_risk_appetite": settings.get("discover_investor_risk_appetite"),
        "monthly_contribution_eur": settings.get("monthly_contribution_eur"),
        "discover_investor_exclusions": settings.get("discover_investor_exclusions"),
    }


def _build_regime_block(db: Session, portfolio_id: str) -> dict[str, Any]:
    """Latest MarketRegime row for this user (table may not exist yet)."""
    portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.id == portfolio_id).one_or_none()
    if not portfolio:
        return {"note": "Portfolio not found"}
    try:
        # MarketRegime may not exist in all deployments; use raw SQL defensively.
        # SAVEPOINT so a missing table doesn't abort the outer Postgres
        # transaction and poison every later query in this session.
        from sqlalchemy import text
        with db.begin_nested():
            result = db.execute(
                text(
                    "SELECT label, confidence, date FROM market_regimes "
                    "WHERE user_id = :user_id ORDER BY date DESC LIMIT 1"
                ),
                {"user_id": portfolio.user_id},
            )
            row = result.fetchone()
        if row:
            return {"label": row[0], "confidence": row[1], "date": str(row[2])}
        return {"note": "No regime data"}
    except Exception as exc:
        logger.debug("Regime block skipped (table may not exist): %s", exc)
        return {"note": "Regime data unavailable"}


def _build_discover_block(db: Session) -> list[dict[str, Any]]:
    """Latest DiscoverCandidate shortlist (stage='shortlisted', limit 10)."""
    try:
        with db.begin_nested():
            rows = (
                db.query(DiscoverCandidate)
                .filter(DiscoverCandidate.status == "shortlisted")
                .order_by(DiscoverCandidate.id.desc())
                .limit(10)
                .all()
            )
        return [
            {
                "symbol": r.symbol,
                "name": r.name,
                "isin": r.isin,
                "scores_json": r.scores_json,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Discover block failed: %s", exc)
        return []


def _assemble_text(blocks: dict[str, Any], max_chars: int) -> str:
    """Concatenate blocks in priority order, truncating lower-priority blocks if needed."""
    priority_order = [
        "assessment",
        "positions",
        "pnl_summary",
        "perf_vs_benchmark",
        "journal",
        "divergence",
        "investor_profile",
        "regime",
        "discover_shortlist",
    ]

    parts: list[str] = []
    used = 0
    for key in priority_order:
        block = blocks.get(key)
        if block is None:
            continue
        text = f"## {key.replace('_', ' ').title()}\n{json.dumps(block, default=str, indent=2)}\n\n"
        if used + len(text) > max_chars:
            # Truncate block
            remaining = max_chars - used
            if remaining > 100:
                text = text[:remaining] + "\n...[truncated]\n\n"
                parts.append(text)
                used += len(text)
            break
        parts.append(text)
        used += len(text)

    return "".join(parts)


# Shared tail of every mandate system prompt: tool list + strict decision
# schema. "rebalance" is deliberately called out as invalid — the mandate
# style text mentions rebalancing thresholds, which primed models to emit
# {"action": "rebalance"}, an action the trade executor cannot act on.
_PROMPT_PROTOCOL = (
    "An `Assessment` section of deterministic facts (concentration, ETF floor, "
    "drawdown, benchmark excess) is provided FIRST — these are computed, not for "
    "you to recompute. Ground your thesis in them and explicitly address any check "
    "with status \"breach\".\n"
    "You have access to tools: get_quote, get_history_stats, get_etf_profile, get_discover_shortlist. "
    "You have a LIMITED number of turns this review — each tool call spends one. "
    "Use only the tool calls you actually need and return the final decision as soon "
    "as you have enough information; running out of turns without deciding forfeits "
    "the review and defaults to holding with no trade. "
    "Reply with EITHER a tool call JSON: {\"tool\": \"...\", \"args\": {...}} "
    "OR the final decision JSON: {\"decision\": {"
    "\"action\": \"buy\"|\"sell\"|\"hold\", \"ticker\": \"...\", \"quantity\": <number>, "
    "\"thesis\": \"...\", "
    "\"alternatives_considered\": [{\"ticker\": \"...\", \"why_rejected\": \"...\"}], "
    "\"key_risks\": [\"...\"], "
    "\"expectation\": {\"metric\": \"abs_return_pct\"|\"benchmark_excess_pct\"|\"max_drawdown_pct\", "
    "\"direction\": \"increase\"|\"decrease\", \"magnitude\": <number>, "
    "\"horizon_weeks\": <integer 1-12>, \"confidence\": <0.0-1.0>}}}. "
    "The `expectation` MUST be measurable: a single metric, a direction, a numeric "
    "magnitude (in percent), and a horizon in weeks — it will be scored against "
    "reality after the horizon elapses. "
    "The action MUST be exactly one of: buy, sell, hold — nothing else. "
    "\"rebalance\" is NOT a valid action: to rebalance, choose the single most "
    "impactful buy or sell for this review; the next weekly review can continue it. "
    "Do not include markdown fences."
)


def _build_system_prompt(mandate: str) -> str:
    """Describe the mandate style for the LLM."""
    if mandate == "A":
        style = (
            "You are a quantitative portfolio manager operating Mandate A — Balanced Improver. "
            "Style: balanced, diversification-first, max 15% in any single position, minimum 40% ETFs, "
            "rebalance when drift exceeds 10%. Prefer steady compounding over aggressive bets. "
            + _PROMPT_PROTOCOL
        )
    elif mandate == "B":
        style = (
            "You are a quantitative portfolio manager operating Mandate B — Momentum Tilted. "
            "Style: momentum-aware, max 20% in any single position, minimum 30% ETFs, "
            "6-month momentum lookback, rebalance when drift exceeds 15%. Tilt toward relative strength "
            "while maintaining a core ETF anchor. "
            + _PROMPT_PROTOCOL
        )
    else:
        style = "You are a quantitative portfolio manager. " + _PROMPT_PROTOCOL
    return style
