"""Divergence analysis between paper portfolio and real holdings."""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.foundation.live_positions import live_positions, manual_holdings_filter
from app.foundation.models.entities import (
    Holding,
    LlmAdviceCard,
    PaperHolding,
    PaperSnapshot,
    Portfolio,
    PortfolioSnapshot,
)

logger = logging.getLogger(__name__)


def compute_divergence(
    db: Session,
    user_id: str,
    paper_portfolio_id: str,
) -> dict[str, Any]:
    """Compare PaperHolding vs real Holding and compute performance delta.

    Matches primarily by ISIN. Mandate/paper holdings frequently have
    isin=None (best-effort resolution failure in review.py's buy path), so
    holdings lacking an ISIN fall back to an uppercase-ticker match against
    real holdings/DKB positions that also lack an ISIN. Each matched/missing
    entry is tagged ``matched_by`` ("isin" or "ticker") for debuggability —
    a ticker match is weaker evidence of "same instrument" than an ISIN
    match (e.g. across exchange-listing variants of the same company).

    Args:
        db: Database session.
        user_id: User UUID.
        paper_portfolio_id: PaperPortfolio UUID.

    Returns:
        Divergence dict with missing_in_real, missing_in_paper, weight_diffs, perf_delta.
    """
    # Paper holdings
    paper_holdings = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == paper_portfolio_id)
        .all()
    )
    paper_by_isin = {h.isin: h for h in paper_holdings if h.isin}
    # Ticker fallback: mandate/paper holdings are ticker-native (ISIN is a
    # best-effort resolution that's frequently None — see review.py's
    # _resolve_isin), so ISIN-only matching is dead for most mandate
    # portfolios. Only index holdings lacking an ISIN here so ISIN-matched
    # entries aren't double-counted via both indexes.
    paper_by_ticker = {
        h.ticker.upper(): h for h in paper_holdings if h.ticker and not h.isin
    }

    # Real holdings: the hand-entered rows of the user's real portfolio(s)
    # plus every synced broker position (DKB, Scalable). The synced copies in
    # the holdings table mirror those positions and are skipped, so nothing
    # is counted twice.
    real_by_isin: dict[str, dict[str, Any]] = {}
    real_by_ticker: dict[str, dict[str, Any]] = {}

    def _add_real(isin: str | None, ticker: str | None, name: str | None, quantity: Decimal) -> None:
        if isin:
            entry = real_by_isin.setdefault(isin, {"ticker": ticker, "name": name, "quantity": Decimal("0")})
        elif ticker:
            entry = real_by_ticker.setdefault(
                ticker.upper(), {"ticker": ticker, "name": name, "quantity": Decimal("0")}
            )
        else:
            return
        entry["quantity"] += quantity

    manual = (
        db.query(Holding)
        .join(Portfolio, Holding.portfolio_id == Portfolio.id)
        .filter(Portfolio.user_id == user_id, manual_holdings_filter())
        .all()
    )
    for h in manual:
        _add_real(h.isin, h.ticker, h.name, h.quantity)
    for pos in live_positions(db, user_id):
        _add_real(pos.isin or None, pos.ticker, pos.name, pos.quantity)

    missing_in_real = []
    missing_in_paper = []
    weight_diffs = []
    matched_real_isins: set[str] = set()
    matched_real_tickers: set[str] = set()

    for isin, ph in paper_by_isin.items():
        if isin not in real_by_isin:
            missing_in_real.append({
                "isin": isin,
                "ticker": ph.ticker,
                "name": ph.name,
                "quantity": str(ph.quantity),
                "matched_by": "isin",
            })
        else:
            matched_real_isins.add(isin)
            rh = real_by_isin[isin]
            qty_diff = float(ph.quantity) - float(rh["quantity"])
            weight_diffs.append({
                "isin": isin,
                "ticker": ph.ticker,
                "paper_quantity": str(ph.quantity),
                "real_quantity": str(rh["quantity"]),
                "quantity_diff": qty_diff,
                "matched_by": "isin",
            })

    for ticker, ph in paper_by_ticker.items():
        if ticker not in real_by_ticker:
            missing_in_real.append({
                "isin": None,
                "ticker": ph.ticker,
                "name": ph.name,
                "quantity": str(ph.quantity),
                "matched_by": "ticker",
            })
        else:
            matched_real_tickers.add(ticker)
            rh = real_by_ticker[ticker]
            qty_diff = float(ph.quantity) - float(rh["quantity"])
            weight_diffs.append({
                "isin": None,
                "ticker": ph.ticker,
                "paper_quantity": str(ph.quantity),
                "real_quantity": str(rh["quantity"]),
                "quantity_diff": qty_diff,
                "matched_by": "ticker",
            })

    for isin, rh in real_by_isin.items():
        if isin not in matched_real_isins:
            missing_in_paper.append({
                "isin": isin,
                "ticker": rh["ticker"],
                "name": rh["name"],
                "quantity": str(rh["quantity"]),
                "matched_by": "isin",
            })

    for ticker, rh in real_by_ticker.items():
        if ticker not in matched_real_tickers:
            missing_in_paper.append({
                "isin": None,
                "ticker": rh["ticker"],
                "name": rh["name"],
                "quantity": str(rh["quantity"]),
                "matched_by": "ticker",
            })

    # Performance delta: paper NAV growth minus real NAV growth over the SAME
    # window, computed from snapshot series. The previous implementation used
    # the paper summary's since-inception total_return_pct — the phantom
    # since-inception figure (see PR #125 root cause) — against a real return
    # over a different basis, which produced absurd deltas (+811%). Comparing
    # NAV growth over the overlapping snapshot window is basis-consistent.
    perf_delta = 0.0
    perf_window: dict[str, Any] = {}
    try:
        paper_snaps = (
            db.query(PaperSnapshot)
            .filter(PaperSnapshot.portfolio_id == paper_portfolio_id)
            .order_by(PaperSnapshot.date.asc())
            .all()
        )
        # Real snapshots: prefer dkb_mirror over computed on the same date.
        source_priority = case(
            (PortfolioSnapshot.source == "dkb_mirror", 0),
            else_=1,
        )
        real_snaps = (
            db.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.user_id == user_id,
                PortfolioSnapshot.source.in_(["dkb_mirror", "computed"]),
            )
            .order_by(PortfolioSnapshot.date.asc(), source_priority.asc())
            .all()
        )
        paper_by_date = {
            s.date: float(s.total_value)
            for s in paper_snaps
            if s.total_value is not None and float(s.total_value) > 0
        }
        real_by_date: dict[Any, float] = {}
        for s in real_snaps:
            if s.total_value is None or float(s.total_value) <= 0:
                continue
            # First (highest-priority) snapshot wins per date.
            real_by_date.setdefault(s.date, float(s.total_value))

        common_dates = sorted(set(paper_by_date) & set(real_by_date))
        if len(common_dates) >= 2:
            start, end = common_dates[0], common_dates[-1]
            paper_growth = paper_by_date[end] / paper_by_date[start] - 1.0
            real_growth = real_by_date[end] / real_by_date[start] - 1.0
            perf_delta = paper_growth - real_growth
            perf_window = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "paper_return": round(paper_growth, 6),
                "real_return": round(real_growth, 6),
                "n_common_dates": len(common_dates),
            }
        else:
            perf_window = {
                "note": "insufficient overlapping snapshots — perf_delta pending",
                "n_common_dates": len(common_dates),
            }
    except Exception as exc:
        logger.debug("Perf delta calculation failed: %s", exc)

    return {
        "missing_in_real": missing_in_real,
        "missing_in_paper": missing_in_paper,
        "weight_diffs": weight_diffs,
        "perf_delta": perf_delta,
        "perf_window": perf_window,
        "paper_portfolio_id": paper_portfolio_id,
        "user_id": user_id,
    }


def generate_advice_cards(
    db: Session,
    user_id: str,
    paper_portfolio_id: str,
) -> list[LlmAdviceCard]:
    """Create LlmAdviceCard rows if divergence exceeds threshold.

    Args:
        db: Database session.
        user_id: User UUID.
        paper_portfolio_id: PaperPortfolio UUID.

    Returns:
        List of created LlmAdviceCard rows (may be empty).
    """
    divergence = compute_divergence(db, user_id, paper_portfolio_id)
    perf_delta = float(divergence.get("perf_delta", 0.0))
    created: list[LlmAdviceCard] = []

    if abs(perf_delta) > 0.05:
        advice_text = (
            f"Paper portfolio divergence detected (performance delta {perf_delta:.2%}). "
            "Positions or returns differ materially from your real holdings. "
            "Review recommended. Any trades are never automated — confirm at DKB yourself."
        )
        card = LlmAdviceCard(
            user_id=user_id,
            portfolio_id=paper_portfolio_id,
            divergence_json=json.dumps(divergence),
            advice_text=advice_text,
            performance_delta=Decimal(str(perf_delta)),
            status="new",
        )
        db.add(card)
        db.commit()
        db.refresh(card)
        created.append(card)
        logger.info("Created advice card for portfolio %s delta %s", paper_portfolio_id, perf_delta)

    return created
