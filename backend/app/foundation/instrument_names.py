"""One name resolution for paper orders: fees, gates and holdings all use it."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.foundation.asset_lookup import find_asset


def resolve_instrument_name(db: Session, ticker: str, isin: str | None = None) -> str:
    """Best-effort real instrument name for a new ``PaperHolding`` row.

    Bypass fix (F9, #2): callers previously wrote ``name=ticker.upper()`` —
    a mangled ticker string, not a real name — which also starves
    ``classify_instrument``'s name-based money-market detection for any
    downstream re-classification (e.g. ``resolve_paper_asset_type`` above,
    or ``advisor/decision.py``'s book-holding classification) that reads
    ``PaperHolding.name`` later. Same lookup order as
    ``resolve_paper_asset_type``: the ``Asset`` registry, then the most
    recent Discover-sourced candidate name. Falls back to the ticker only
    when neither source has anything — unchanged behaviour for tickers with
    no available metadata.
    """
    from app.foundation.models.entities import DiscoverCandidate

    ticker_u = ticker.upper()
    asset = find_asset(db, isin=isin, symbol=ticker_u)
    if asset is not None and asset.name:
        return asset.name

    cand = (
        db.query(DiscoverCandidate.name)
        .filter(DiscoverCandidate.symbol == ticker_u, DiscoverCandidate.name.isnot(None))
        .order_by(DiscoverCandidate.id.desc())
        .first()
    )
    if cand and cand[0]:
        return cand[0]
    return ticker_u
