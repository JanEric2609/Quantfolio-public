"""ETF overlap analysis.

Computes pairwise holding overlap and portfolio-level overlap matrices.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Holding, Portfolio
from app.foundation.etf_lookup import get_etf_composition

logger = logging.getLogger(__name__)


def pairwise_overlap(
    holdings_a: list[dict[str, Any]],
    holdings_b: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute overlap metrics between two ETF holdings lists.

    Each holdings list is a list of dicts with at least ``ticker`` and
    ``weight`` keys (weight as a decimal, e.g. 0.05 for 5%).

    Returns:
        - ``overlap`` (float): sum of min(weights) — the classic overlap metric
        - ``jaccard`` (float): |intersection| / |union| based on tickers
        - ``common_holdings`` (list): shared tickers with both weights
        - ``total_a``, ``total_b`` (int): number of holdings in each list
    """
    weights_a = {h["ticker"].upper(): float(h.get("weight", 0)) for h in holdings_a if h.get("ticker")}
    weights_b = {h["ticker"].upper(): float(h.get("weight", 0)) for h in holdings_b if h.get("ticker")}

    common = set(weights_a.keys()) & set(weights_b.keys())
    union = set(weights_a.keys()) | set(weights_b.keys())

    overlap = sum(min(weights_a.get(t, 0), weights_b.get(t, 0)) for t in common)
    jaccard = len(common) / len(union) if union else 0.0

    common_holdings = [
        {
            "ticker": t,
            "weight_a": round(weights_a[t], 4),
            "weight_b": round(weights_b[t], 4),
            "min_weight": round(min(weights_a[t], weights_b[t]), 4),
        }
        for t in sorted(common)
    ]

    return {
        "overlap": round(overlap, 4),
        "jaccard": round(jaccard, 4),
        "common_holdings": common_holdings,
        "total_a": len(weights_a),
        "total_b": len(weights_b),
    }


def portfolio_overlap(db: Session, user_id: str) -> dict[str, Any] | None:
    """Build an overlap matrix for all ETF holdings in a user's portfolio.

    Returns a dict with:
        - ``etfs`` (list): tickers / names of held ETFs
        - ``matrix`` (list of lists): overlap values (row i, col j)
        - ``jaccard_matrix`` (list of lists): Jaccard similarities
        - ``details`` (list): pairwise detail records
    """
    portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
    if not portfolio:
        return None

    holdings = (
        db.query(Holding)
        .filter(
            Holding.portfolio_id == portfolio.id,
            Holding.asset_type == "etf",
        )
        .all()
    )

    if len(holdings) < 2:
        return {
            "etfs": [],
            "matrix": [],
            "jaccard_matrix": [],
            "details": [],
            "message": "Need at least two ETF holdings to compute overlap.",
        }

    # Fetch composition for each held ETF
    etf_data: list[dict[str, Any]] = []
    for h in holdings:
        ticker = (h.ticker or h.isin or "").upper().strip()
        if not ticker:
            continue
        composition = get_etf_composition(ticker)
        if composition and composition.holdings:
            holdings_list = [
                {"ticker": hh.ticker, "weight": hh.weight}
                for hh in composition.holdings
            ]
        else:
            holdings_list = []
        etf_data.append({
            "ticker": ticker,
            "name": h.name or composition.name if composition else ticker,
            "holdings": holdings_list,
        })

    if len(etf_data) < 2:
        return {
            "etfs": [e["ticker"] for e in etf_data],
            "matrix": [],
            "jaccard_matrix": [],
            "details": [],
            "message": "Could not fetch holdings data for overlap calculation.",
        }

    n = len(etf_data)
    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    jaccard_matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    details: list[dict[str, Any]] = []

    for i in range(n):
        for j in range(i + 1, n):
            result = pairwise_overlap(etf_data[i]["holdings"], etf_data[j]["holdings"])
            matrix[i][j] = result["overlap"]
            matrix[j][i] = result["overlap"]
            jaccard_matrix[i][j] = result["jaccard"]
            jaccard_matrix[j][i] = result["jaccard"]
            details.append({
                "etf_a": etf_data[i]["ticker"],
                "name_a": etf_data[i]["name"],
                "etf_b": etf_data[j]["ticker"],
                "name_b": etf_data[j]["name"],
                **result,
            })

    return {
        "etfs": [{"ticker": e["ticker"], "name": e["name"]} for e in etf_data],
        "matrix": matrix,
        "jaccard_matrix": jaccard_matrix,
        "details": details,
    }
