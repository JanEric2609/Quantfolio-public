"""Portfolio-Level LLM Analysis — synthesizes individual stock reports into a
cohesive portfolio view.  Covers allocation, regime-aware recommendations,
rebalancing suggestions, concentration risks, and macro context.

Generated weekly (Sunday 9:30 AM) and cached for 7 days.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.foundation.portfolio.name_resolver import resolve_position_name
from app.foundation.text_safety import ascii_safe

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    StockResearchReport,
    User,
    WatchlistItem,
)
from app.foundation.market import quote

logger = logging.getLogger(__name__)

PORTFOLIO_TICKER = "_PORTFOLIO"  # sentinel ticker for portfolio-level reports
PORTFOLIO_REPORT_TTL_HOURS = 168  # 7 days


# ---------------------------------------------------------------------------
# Data-gathering helpers
# ---------------------------------------------------------------------------

def _portfolio_holdings(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Gather all portfolio holdings (manual + DKB) via wealth_summary."""
    from app.foundation.portfolio_service import wealth_summary
    summary = wealth_summary(db, user_id)
    return summary.get("positions", [])


def _individual_stock_reports(db: Session, user_id: str) -> dict[str, Any]:
    """Fetch the latest cached per-stock reports for all tickers."""
    tickers = (
        db.query(WatchlistItem.ticker)
        .filter(WatchlistItem.user_id == user_id)
        .all()
    )
    ticker_list = [t[0] for t in tickers if t[0]]

    # Also include tickers from holdings
    from app.foundation.live_positions import live_positions
    from app.foundation.models.entities import Holding, Portfolio
    portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
    if portfolio:
        for h in db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all():
            if h.ticker and h.ticker not in ticker_list:
                ticker_list.append(h.ticker)

    for p in live_positions(db, user_id):
        if p.ticker and p.ticker not in ticker_list:
            ticker_list.append(p.ticker)

    reports: dict[str, Any] = {}
    for ticker in ticker_list[:30]:
        row = (
            db.query(StockResearchReport)
            .filter(
                StockResearchReport.ticker == ticker,
                StockResearchReport.user_id == user_id,
                StockResearchReport.expires_at > datetime.now(UTC),
            )
            .order_by(StockResearchReport.generated_at.desc())
            .first()
        )
        if row:
            reports[ticker] = {
                "summary": row.executive_summary,
                "report_preview": (row.report_json or "")[:500],
            }
        else:
            # Try to get a basic quote even without a full report
            try:
                q = quote(db, ticker)
                reports[ticker] = {"quote": q, "summary": None}
            except Exception:
                reports[ticker] = {"summary": None}

    return {"tickers": ticker_list, "reports": reports}


def _current_regime(db: Session) -> str:
    """Return the current market regime label or 'unknown'."""
    try:
        from app.lab.regime.macro_snapshot import get_or_refresh_regime
        regime = get_or_refresh_regime(db)
        return regime.get("label", "unknown") if isinstance(regime, dict) else "unknown"
    except Exception:
        return "unknown"


def _user_risk_profile(db: Session, user_id: str) -> str:
    user = db.get(User, user_id)
    return getattr(user, "risk_profile", "moderate") if user else "moderate"


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def build_portfolio_context(db: Session, user_id: str) -> dict[str, Any]:
    """Assemble all portfolio-level data for the LLM prompt."""
    from app.foundation.portfolio_service import wealth_summary

    portfolio_data = wealth_summary(db, user_id)
    stock_reports = _individual_stock_reports(db, user_id)
    regime = _current_regime(db)
    risk_profile = _user_risk_profile(db, user_id)

    # Compute allocation percentages
    positions = portfolio_data.get("positions", [])
    total_value = portfolio_data.get("total_value", 0)
    allocation = {}
    if total_value > 0:
        for pos in positions:
            asset_type = pos.get("asset_type", pos.get("source", "unknown"))
            value = pos.get("current_value", 0)
            allocation[asset_type] = allocation.get(asset_type, 0) + value
        allocation = {k: round(v / total_value * 100, 1) for k, v in allocation.items()}

    # Compute risk metrics if price data is available
    risk_metrics: dict[str, float] = {}
    try:
        from app.foundation.quant_metrics import (
            sharpe_ratio, sortino_ratio, max_drawdown as calc_max_dd,
            historical_cvar,
        )
        tickers = [p.get("ticker") or p.get("symbol") for p in positions if p.get("ticker") or p.get("symbol")]
        if tickers:
            from app.foundation.portfolio_price_service import portfolio_price_matrix
            matrix, weights = portfolio_price_matrix(db, user_id)
            if matrix:
                import numpy as np
                # Build return series for each ticker with sufficient data
                returns_list = []
                valid_tickers = []
                for t in tickers:
                    if t in matrix and len(matrix[t]) >= 10:
                        prices_dict = matrix[t]
                        sorted_dates = sorted(prices_dict.keys())
                        prices = [prices_dict[d] for d in sorted_dates]
                        rets = np.diff(np.log(np.array(prices, dtype=float)))
                        if len(rets) > 0:
                            returns_list.append(rets)
                            valid_tickers.append(t)
                if returns_list and len(returns_list) >= 2:
                    min_len = min(len(r) for r in returns_list)
                    returns_matrix = np.column_stack([r[:min_len] for r in returns_list])
                    valid_weights = np.array([weights.get(t, 0) for t in valid_tickers])
                    if valid_weights.sum() > 0:
                        valid_weights = valid_weights / valid_weights.sum()
                        port_returns = returns_matrix @ valid_weights
                        risk_metrics = {
                            "sharpe": round(sharpe_ratio(port_returns), 3),
                            "sortino": round(sortino_ratio(port_returns), 3),
                            "max_drawdown": round(calc_max_dd(port_returns).get("max_drawdown", 0.0), 3),
                            "cvar_95": round(historical_cvar(port_returns), 3),
                        }

    except Exception:
        pass

    return {
        "portfolio_summary": {
            "total_value": total_value,
            "cash_value": portfolio_data.get("cash_value", 0),
            "security_value": portfolio_data.get("security_value", 0),
            "currency": portfolio_data.get("currency", "EUR"),
            "allocation_pct": allocation,
            "cashflow_30d": portfolio_data.get("cashflow_30d", {}),
        },
        "positions": [
            {
                "name": resolve_position_name(p, db),
                "ticker": p.get("ticker") or p.get("symbol"),
                "value": p.get("current_value", 0),
                "source": p.get("source", "unknown"),
                "asset_type": p.get("asset_type", "unknown"),
                "pct": round(p.get("current_value", 0) / total_value * 100, 1) if total_value else 0,
            }
            for p in positions
        ],
        "stock_reports": stock_reports,
        "regime": regime,
        "risk_profile": risk_profile,
        "risk_metrics": risk_metrics,
    }


# ---------------------------------------------------------------------------
# Report cache layer
# ---------------------------------------------------------------------------

def get_cached_portfolio_report(db: Session, user_id: str) -> StockResearchReport | None:
    """Return a non-expired cached portfolio report or None."""
    row = (
        db.query(StockResearchReport)
        .filter(
            StockResearchReport.ticker == PORTFOLIO_TICKER,
            StockResearchReport.user_id == user_id,
        )
        .order_by(StockResearchReport.generated_at.desc())
        .first()
    )
    if row and row.expires_at > datetime.now(UTC):
        return row
    return None


def save_portfolio_report(
    db: Session, user_id: str, report_text: str, summary: str, context: dict[str, Any]
) -> StockResearchReport:
    now = datetime.now(UTC)
    report = StockResearchReport(
        ticker=PORTFOLIO_TICKER,
        user_id=user_id,
        report_json=ascii_safe(report_text),
        executive_summary=ascii_safe(summary),
        data_snapshot_json=json.dumps(context, default=str),
        generated_at=now,
        expires_at=now + timedelta(hours=PORTFOLIO_REPORT_TTL_HOURS),
    )
    db.add(report)
    try:
        db.commit()
    except Exception:
        logger.exception("Failed to persist portfolio report, rolling back")
        db.rollback()
        raise
    db.refresh(report)
    return report


# ---------------------------------------------------------------------------
# LLM prompt template
# ---------------------------------------------------------------------------

_PORTFOLIO_SYSTEM_PROMPT = """\
You are a senior portfolio manager and investment strategist.  Given the
portfolio data below, produce a comprehensive portfolio-level analysis.

The report must include these sections:

1. **Portfolio Overview** — total value, asset allocation breakdown, cash position.
2. **Regime Assessment** — current market regime impact on the portfolio.
3. **Concentration Risk** — any positions that are too large relative to total portfolio.
4. **Rebalancing Suggestions** — whether allocation drift warrants rebalancing.
5. **Risk Alignment** — does the current portfolio match the user's risk profile?
6. **Macro Context** — how current market conditions affect this specific portfolio.
7. **Action Items** — prioritized list of concrete next steps.
8. **Weekly Outlook** — key events or themes for the coming week.

Adapt tone to the user's risk profile:
- conservative → emphasize capital preservation and diversification
- moderate → balanced growth with risk awareness
- aggressive → opportunity-focused, comfortable with concentration

Include brief explanations for technical terms.  Be specific to THIS portfolio —
reference actual holdings by name and percentage.  This is NOT investment advice;
it is an analytical perspective for informed decision-making."""


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

async def generate_portfolio_report(
    db: Session, user_id: str, *, force: bool = False
) -> dict[str, Any]:
    """Generate or return cached portfolio-level LLM report.

    Returns ``{"report": str, "summary": str, "cached": bool, "generated_at": str}``.
    """
    if not force:
        cached = get_cached_portfolio_report(db, user_id)
        if cached:
            return {
                "report": cached.report_json,
                "summary": cached.executive_summary,
                "cached": True,
                "generated_at": cached.generated_at.isoformat(),
            }

    context = build_portfolio_context(db, user_id)

    # Build user message with all context
    user_msg_parts = [
        "## Portfolio Summary",
        f"- Total Value: {context['portfolio_summary']['currency']} {context['portfolio_summary']['total_value']:,.2f}",
        f"- Cash: {context['portfolio_summary']['currency']} {context['portfolio_summary']['cash_value']:,.2f}",
        f"- Securities: {context['portfolio_summary']['currency']} {context['portfolio_summary']['security_value']:,.2f}",
        f"- Allocation: {json.dumps(context['portfolio_summary']['allocation_pct'])}",
        f"- 30-day Cashflow: {json.dumps(context['portfolio_summary']['cashflow_30d'])}",
        "",
        f"## Current Regime: {context['regime']}",
        f"## User Risk Profile: {context['risk_profile']}",
        "",
        "## Positions",
    ]
    for pos in context["positions"]:
        user_msg_parts.append(
            f"- {pos['name']} ({pos['ticker'] or 'N/A'}): "
            f"{context['portfolio_summary']['currency']} {pos['value']:,.2f} "
            f"({pos['pct']}%) — {pos['asset_type']}"
        )

    # Inject risk metrics if available
    risk_metrics = context.get("risk_metrics", {})
    if risk_metrics:
        user_msg_parts.extend([
            "",
            "## Portfolio Risk Metrics",
            f"- Sharpe Ratio: {risk_metrics.get('sharpe', 'N/A')}",
            f"- Sortino Ratio: {risk_metrics.get('sortino', 'N/A')}",
            f"- Max Drawdown: {risk_metrics.get('max_drawdown', 'N/A')}",
            f"- CVaR (95%): {risk_metrics.get('cvar_95', 'N/A')}",
        ])

    user_msg_parts.extend(["", "## Individual Stock Reports"])
    for ticker, data in context["stock_reports"].get("reports", {}).items():
        summary = data.get("summary")
        if summary:
            user_msg_parts.append(f"- **{ticker}**: {summary}")
        elif data.get("quote"):
            q = data["quote"]
            user_msg_parts.append(f"- **{ticker}**: price {q.get('price', 'N/A')}, change {q.get('change_pct', 'N/A')}%")
        else:
            user_msg_parts.append(f"- **{ticker}**: no data available")

    user_msg = "\n".join(user_msg_parts)

    # Call LLM
    from app.foundation.llm.router import call as llm_call
    messages = [
        {"role": "system", "content": _PORTFOLIO_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        completion = await llm_call(db, "batch_research", messages, timeout_s=120.0, user_id=user_id)
        report_text = completion.content

        # Extract first paragraph as summary
        lines = report_text.strip().split("\n")
        summary = ""
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                summary = stripped[:200]
                break
        if not summary:
            summary = report_text[:200]

        try:
            saved = save_portfolio_report(db, user_id, report_text, summary, context)
            persisted = True
        except Exception as persist_exc:
            logger.exception("Portfolio report persist failed: %s", persist_exc)
            db.rollback()
            persisted = False
            saved = None

        return {
            "report": report_text,
            "summary": summary,
            "cached": False,
            "persisted": persisted,
            "generated_at": (saved.generated_at.isoformat() if saved else datetime.now(UTC).isoformat()),
        }
    except Exception as exc:
        logger.exception("Portfolio report generation failed: %s", exc)
        db.rollback()
        # Return partial data without LLM opinion
        return {
            "report": "",
            "summary": "Report generation unavailable. Portfolio data is shown below.",
            "cached": False,
            "persisted": False,
            "generated_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "context": context,
        }


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------

def run_weekly_portfolio_analysis() -> int:
    """Entry point for the weekly portfolio analysis worker job.

    Returns the number of reports generated.
    """
    from app.foundation.core.db import SessionLocal
    from app.foundation.models.entities import User

    count = 0
    # Get user IDs with a short-lived session, then process each user separately
    # to avoid one user's failure or long-running LLM call blocking others.
    user_ids: list[str] = []
    with SessionLocal() as db:
        user_ids = [u.id for u in db.query(User).all()]

    for uid in user_ids:
        try:
            import asyncio
            with SessionLocal() as db:
                result = asyncio.run(generate_portfolio_report(db, uid))
                if result.get("report"):
                    count += 1
        except Exception as exc:
            logger.warning("Portfolio analysis failed for user %s: %s", uid, exc)
    return count
