"""One ``Asset`` row for an ISIN or a symbol, even when several listings share it.

``assets`` is unique only on ``(isin, exchange, currency)``: one ISIN can have a
Xetra and a London row (EUNL.DE / IWDA.L), and a symbol can repeat across
exchanges. ``.one_or_none()`` on the ISIN or symbol alone then raises
``MultipleResultsFound`` (audit 2026-11-15, 2.1: it crashed the Mandate B review)
and ``.first()`` without an order picks an arbitrary listing.

:func:`find_asset` picks one row deterministically:

1. the listing whose symbol is the one the caller holds, if given;
2. a EUR listing;
3. the alphabetically first symbol, then the row id.
"""

from __future__ import annotations

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.foundation.models.entities import Asset


def _ordered(query, symbol: str | None):
    order = []
    if symbol:
        order.append(case((Asset.symbol == symbol.upper(), 0), else_=1))
    order.append(case((Asset.currency == "EUR", 0), else_=1))
    order.extend([Asset.symbol, Asset.id])
    return query.order_by(*order)


def find_asset(
    db: Session,
    *,
    isin: str | None = None,
    symbol: str | None = None,
    symbol_fallback: bool = True,
) -> Asset | None:
    """Return the best ``Asset`` for ``isin`` (preferring ``symbol``'s listing).

    With no ISIN, or no row for it, the symbol alone is looked up when
    ``symbol_fallback`` is true. Never raises on duplicates.
    """
    if isin:
        row = _ordered(db.query(Asset).filter(Asset.isin == isin), symbol).first()
        if row is not None or not symbol_fallback:
            return row
    if symbol:
        return _ordered(db.query(Asset).filter(Asset.symbol == symbol.upper()), None).first()
    return None


def local_isin_for_ticker(db: Session, ticker: str) -> str | None:
    """The ISIN stored anywhere for ``ticker`` (no network), or None.

    Looks at the ``Asset`` registry, Discover candidates, paper holdings of
    the same ticker in other sleeves, the index members (MSCI World: ISIN
    from the fund's holdings file, symbol via OpenFIGI and confirmed by
    prices) and the security master, in that order. Yahoo's own ISIN is junk
    for some European names (ENI.MI "NET000PLEN01", TTE.PA "NETTOTALOIL1"),
    so the index members are the only source for them (check-in 2026-11-17).
    """
    from app.foundation.models.entities import DiscoverCandidate, IndexUniverseMember, PaperHolding

    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    asset = _ordered(db.query(Asset).filter(Asset.symbol == sym, Asset.isin.isnot(None)), None).first()
    if asset is not None and asset.isin:
        return asset.isin
    candidate = (
        db.query(DiscoverCandidate.isin)
        .filter(DiscoverCandidate.symbol == sym, DiscoverCandidate.isin.isnot(None))
        .order_by(DiscoverCandidate.id.desc())
        .first()
    )
    if candidate is not None and candidate[0]:
        return str(candidate[0])
    held = (
        db.query(PaperHolding.isin)
        .filter(PaperHolding.ticker == sym, PaperHolding.isin.isnot(None))
        .order_by(PaperHolding.isin)
        .first()
    )
    if held is not None and held[0]:
        return str(held[0])
    member = (
        db.query(IndexUniverseMember.isin)
        .filter(IndexUniverseMember.symbol == sym, IndexUniverseMember.symbol_ok.is_(True))
        .order_by(IndexUniverseMember.index_key, IndexUniverseMember.isin)
        .first()
    )
    if member is not None and member[0]:
        return str(member[0])
    try:
        from app.foundation.data_engineering.pit_fundamentals import resolve_isin_for_symbol

        return resolve_isin_for_symbol(db, sym)
    except Exception:  # noqa: BLE001 - the security master is optional evidence
        return None


def yfinance_isin(ticker: str) -> str | None:
    """yfinance's ISIN for ``ticker`` when its check digit holds, else None."""
    from app.foundation.security_master import valid_isin

    try:
        import yfinance as yf

        value = yf.Ticker(ticker).isin
    except Exception:  # noqa: BLE001 - network / provider failure: no ISIN
        return None
    return value if valid_isin(value) else None


def resolve_isin_for_ticker(db: Session, ticker: str, *, allow_network: bool = True) -> str | None:
    """:func:`local_isin_for_ticker`, then yfinance when ``allow_network``."""
    isin = local_isin_for_ticker(db, ticker)
    if isin is None and allow_network:
        isin = yfinance_isin(ticker)
    return isin
