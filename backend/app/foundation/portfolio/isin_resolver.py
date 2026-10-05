"""ISIN \u2192 ticker resolution for DKB positions.

Resolution chain:

1. ``isin_ticker_overrides`` public setting (JSON dict ISIN \u2192 ticker),
   layered over the built-in :data:`CANONICAL_ISIN_TICKERS`
2. Direct ticker shortcut (all-alpha, \u22645 chars)
3. ``yfinance.Ticker(isin).info`` \u2192 ``symbol``
4. ``EtfUniverseProvider.lookup_by_isin`` \u2192 ``symbol``

Explicit mappings come first: yfinance picks an arbitrary listing (for
IE00B4L5Y983 it returns the USD-quoted London line ``IWDA.L``), so an
override consulted after it could never correct a mis-resolution.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.foundation.models.entities import BrokerPosition, DkbAccount, DkbPosition

logger = logging.getLogger(__name__)

# Built-in ISIN -> ticker pins, applied before any automatic lookup. The
# ``isin_ticker_overrides`` setting is layered on top and wins on conflict.
# IE00B4L5Y983 (iShares Core MSCI World) resolves via yfinance to IWDA.L,
# which quotes in USD; the paper book and every EUR-return computation then
# silently ignore FX. EUNL.DE is the same fund on XETRA in EUR, the venue a
# DKB savings plan executes on, and the canonical listing per DEC-A
# (docs/archive/audits/2026-08-universe-regime-audit.md).
CANONICAL_ISIN_TICKERS: dict[str, str] = {
    "IE00B4L5Y983": "EUNL.DE",
}


def isin_ticker_overrides(db: Session | None) -> dict[str, str]:
    """Return the effective ISIN -> ticker pins (built-in + user setting)."""
    merged = dict(CANONICAL_ISIN_TICKERS)
    if db is None:
        return merged
    try:
        from app.foundation.settings import get_public_settings

        overrides = get_public_settings(db).get("isin_ticker_overrides") or {}
        if isinstance(overrides, str):
            overrides = json.loads(overrides)
        if isinstance(overrides, dict):
            merged.update({str(k).upper(): str(v).upper() for k, v in overrides.items() if v})
    except Exception:
        logger.debug("isin_ticker_overrides lookup failed", exc_info=True)
    return merged


def resolve_isin_to_ticker(db: Session, user_id: str) -> dict[str, Any]:
    """Attempt to resolve tickers for synced positions that only have an ISIN.

    Covers DKB positions and every broker in ``broker_positions`` (Scalable
    Capital). Also re-points positions whose stored ticker disagrees with an
    explicit pin. Returns a dict with ``resolved``, ``unresolved``,
    ``remapped`` and ``details`` keys.
    """
    accounts = db.execute(
        select(DkbAccount.id).where(DkbAccount.user_id == user_id)
    ).scalars().all()
    broker_rows = db.execute(
        select(BrokerPosition).where(BrokerPosition.user_id == user_id)
    ).scalars().all()

    if not accounts and not broker_rows:
        return {"resolved": 0, "unresolved": 0, "remapped": 0, "details": []}

    # Re-point positions whose stored ticker disagrees with an explicit pin
    # (e.g. IWDA.L resolved by yfinance before the pin existed). Runs every
    # price refresh, so a new override takes effect without a DKB re-sync.
    pins = isin_ticker_overrides(db)
    remapped = 0
    details: list[dict[str, Any]] = []
    pinned_positions: list[Any] = []
    if accounts:
        pinned_positions = list(db.execute(
            select(DkbPosition).where(
                DkbPosition.account_id.in_(accounts),
                DkbPosition.isin.in_(list(pins)),
            )
        ).scalars().all())
    pinned_positions += [p for p in broker_rows if p.isin and p.isin.upper() in pins]
    for pos in pinned_positions:
        target = pins[pos.isin.upper()]
        if pos.ticker and pos.ticker.upper() != target:
            details.append({"isin": pos.isin, "ticker": target, "previous": pos.ticker, "status": "remapped"})
            pos.ticker = target
            remapped += 1
    if remapped:
        db.commit()
        logger.info("Re-pointed %d position(s) to pinned tickers for user %s", remapped, user_id)

    positions: list[Any] = []
    if accounts:
        positions = list(db.execute(
            select(DkbPosition).where(
                DkbPosition.account_id.in_(accounts),
                or_(DkbPosition.ticker.is_(None), DkbPosition.ticker == ""),
            )
        ).scalars().all())
    positions += [p for p in broker_rows if not p.ticker]

    if not positions:
        return {"resolved": 0, "unresolved": 0, "remapped": remapped, "details": details}

    # A ticker another synced position already carries for the same ISIN is
    # reused before any network lookup, so both brokers price off one listing.
    known: dict[str, str] = {}
    if accounts:
        for isin, ticker in db.execute(
            select(DkbPosition.isin, DkbPosition.ticker).where(
                DkbPosition.account_id.in_(accounts), DkbPosition.ticker.is_not(None), DkbPosition.ticker != ""
            )
        ).all():
            known.setdefault(str(isin).upper(), ticker)
    for p in broker_rows:
        if p.ticker:
            known.setdefault(p.isin.upper(), p.ticker)

    resolved = 0
    unresolved = 0

    for pos in positions:
        ticker = known.get((pos.isin or "").upper()) or _try_resolve_isin(pos.isin, db)
        if ticker:
            pos.ticker = ticker
            known.setdefault((pos.isin or "").upper(), ticker)
            resolved += 1
            details.append({"isin": pos.isin, "ticker": ticker, "status": "resolved"})
        else:
            unresolved += 1
            details.append({"isin": pos.isin, "ticker": None, "status": "unresolved"})

    if resolved:
        db.commit()
        logger.info(
            "Resolved %d ISIN\u2192ticker for user %s (%d unresolved)",
            resolved,
            user_id,
            unresolved,
        )

    return {"resolved": resolved, "unresolved": unresolved, "remapped": remapped, "details": details}


def _try_resolve_isin(isin: str, db: Session | None = None) -> str | None:
    """Resolve an ISIN to a tradable ticker using a multi-step chain.

    1. Explicit pins \u2014 :func:`isin_ticker_overrides` (built-in + setting).
    2. Direct ticker shortcut \u2014 if *isin* looks like a ticker, return it.
    3. yfinance ``yf.Ticker(isin).info`` \u2192 ``symbol``.
    4. ``EtfUniverseProvider().lookup_by_isin(isin)`` \u2192 ``symbol``.

    Returns uppercase ticker or ``None``.
    """
    if not isin:
        return None

    # 1. Explicit pins win over every automatic lookup.
    pinned = isin_ticker_overrides(db).get(isin.upper())
    if pinned:
        return pinned

    # 2. If it already looks like a ticker (no digits, short), trust it.
    if isin.isalpha() and len(isin) <= 5:
        return isin.upper()

    # 3. yfinance ISIN lookup
    try:
        import yfinance as yf

        info = yf.Ticker(isin).info
        symbol = info.get("symbol")
        if symbol:
            return symbol.upper()
    except Exception:
        logger.debug("yfinance ISIN lookup failed for %s", isin, exc_info=True)

    # 4. justETF universe lookup
    try:
        from app.foundation.etf_universe import EtfUniverseProvider

        etf = EtfUniverseProvider().lookup_by_isin(isin)
        if etf and etf.get("symbol"):
            return etf["symbol"].upper()
    except Exception:
        logger.debug("ETF universe lookup failed for %s", isin, exc_info=True)

    return None
