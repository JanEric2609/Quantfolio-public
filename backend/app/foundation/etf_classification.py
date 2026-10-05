"""ETF classification and asset enrichment service.

Queries a user's portfolio holdings, looks up ISINs against the justETF
universe, and upserts enriched ``Asset`` rows. Falls back to yfinance for
unknown ISINs.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Asset, Holding
from app.foundation.asset_enrichment import classify_isin
from app.foundation.etf_universe import EtfUniverseProvider
from app.foundation.portfolio_service import main_portfolio

logger = logging.getLogger(__name__)


def classify_and_enrich(db: Session, user_id: str) -> dict[str, Any]:
    """Classify and enrich assets for a user's portfolio holdings.

    1. Fetches the user's main portfolio and holdings.
    2. For each holding with an ISIN, attempts lookup via
       ``EtfUniverseProvider.lookup_by_isin``.
    3. If found in justETF → upserts an ``Asset`` row with ETF metadata.
    4. If not found → falls back to ``yfinance.Ticker(isin).info`` to guess
       ``asset_type`` (etf / stock / other).
    5. Propagates each Asset's authoritative ``asset_type`` onto every existing
       Holding row with a matching ISIN.
    6. Returns a summary dict with counts and any non-fatal errors.

    Parameters
    ----------
    db:
        SQLAlchemy session.
    user_id:
        UUID of the user whose portfolio should be enriched.

    Returns
    -------
    dict
        ``{"classified": int, "updated": int, "errors": list[str]}``
    """
    classified = 0
    updated = 0
    errors: list[str] = []

    portfolio = main_portfolio(db, user_id)
    holdings = db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all()

    if not holdings:
        return {"classified": 0, "updated": 0, "errors": []}

    provider = EtfUniverseProvider()

    unique_isins = {h.isin for h in holdings if h.isin}
    for isin in unique_isins:
        try:
            result = classify_isin(db, provider, isin)
            if result:
                classified += 1
            else:
                updated += 1
        except Exception as exc:
            msg = f"Failed to classify ISIN {isin}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    propagate_asset_types(db, sorted(unique_isins))
    return {"classified": classified, "updated": updated, "errors": errors}


def propagate_asset_types(db: Session, isins: list[str]) -> dict[str, int]:
    """Propagate each Asset's authoritative ``asset_type`` onto Holding rows.

    Closes the misclassification loop: upserting an ``Asset`` row used to fix
    only future creations while pre-existing Holdings kept their stale
    ``asset_type`` forever. For every ISIN backed by an Asset row, all Holding
    rows carrying that ISIN — ``dkb_sync`` and manual alike — are re-typed.
    ISINs without Asset evidence (and holdings without an ISIN) stay untouched
    (DEC-E: auto-repair only where ISIN↔Asset evidence exists).

    Returns a per-ISIN map of how many Holding rows were actually changed, so
    callers (e.g. the Wave-2 admin repair endpoint) can report and re-run
    idempotently.
    """
    counts: dict[str, int] = {}
    for isin in dict.fromkeys(isins):  # de-dup, preserve order
        if not isin:
            continue
        asset = db.query(Asset).filter(Asset.isin == isin).first()
        if asset is None:
            counts[isin] = 0
            continue
        changed = 0
        for holding in db.query(Holding).filter(Holding.isin == isin).all():
            if holding.asset_type != asset.asset_type:
                holding.asset_type = asset.asset_type
                changed += 1
        counts[isin] = changed
    db.flush()
    return counts


