"""Portfolio-independent ``Asset`` row enrichment by ISIN.

Foundation-layer module (no decision-loop dependency) so both
``etf_classification`` (portfolio-holdings-scoped) and ``discover`` (a
decision-loop context) can populate/read ``assets`` from a bare ISIN without
``discover`` importing through ``etf_classification`` -> ``portfolio_service``
-> ``app.foundation.portfolio`` — which would violate the "decision-loop
packages are independent" import-linter contract (ADR 0014 §6: this module
was split out for exactly that reason, when wiring the Discover dossier
writer to ground its prompt in real holdings/metadata).

Lookup order: justETF universe cache first, yfinance fallback second — same
priority ``etf_classification._classify_single`` used before this split.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Asset, uuid_pk
from app.foundation.etf_universe import EtfUniverseProvider

logger = logging.getLogger(__name__)


def upsert_asset_by_isin(db: Session, isin: str) -> Asset | None:
    """Upsert (or fetch) an ``Asset`` row for an arbitrary ISIN.

    Best-effort: returns ``None`` on total lookup failure rather than
    raising — callers must not let a data-source outage block whatever
    they're doing (e.g. dossier generation).
    """
    if not isin:
        return None
    provider = EtfUniverseProvider()
    try:
        classify_isin(db, provider, isin)
    except Exception as exc:
        logger.debug("upsert_asset_by_isin failed for %s: %s", isin, exc)
        return None
    return db.query(Asset).filter(Asset.isin == isin).first()


def classify_isin(
    db: Session,
    provider: EtfUniverseProvider,
    isin: str,
) -> bool:
    """Classify a single ISIN and upsert an ``Asset`` row.

    Returns ``True`` if a *new* Asset was inserted, ``False`` if an existing
    row was updated (or no change was needed).
    """
    etf_data = provider.lookup_by_isin(isin)

    if etf_data:
        return _upsert_from_justetf(db, isin, etf_data)

    # Fallback: yfinance
    try:
        import yfinance as yf

        info = yf.Ticker(isin).info
    except Exception:
        logger.debug("yfinance fallback failed for ISIN %s", isin)
        return False

    quote_type = info.get("quoteType", "").upper()
    if quote_type == "ETF":
        asset_type = "etf"
    elif quote_type in ("EQUITY", "STOCK"):
        asset_type = "stock"
    else:
        asset_type = "other"

    symbol = info.get("symbol") or info.get("ticker") or None
    name = info.get("longName") or info.get("shortName") or isin
    currency = info.get("currency") or "EUR"
    exchange = info.get("exchange") or "UNKNOWN"
    country = info.get("country") or None

    return _upsert_asset(
        db,
        isin=isin,
        symbol=symbol,
        exchange=exchange,
        name=name,
        asset_type=asset_type,
        currency=currency,
        country=country,
        ucits=False,
        accumulating=None,
        distributing=None,
        ter=None,
        provider_meta_json=json.dumps({"yfinance_quote_type": quote_type}),
    )


def _upsert_from_justetf(
    db: Session,
    isin: str,
    etf_data: dict[str, Any],
) -> bool:
    """Upsert an ``Asset`` from a justETF record.

    Returns ``True`` if a new row was inserted.
    """
    symbol = etf_data.get("symbol")
    name = etf_data.get("name") or isin
    currency = etf_data.get("currency") or "EUR"
    country = etf_data.get("domicile_country") or None
    ter_val = etf_data.get("ter")
    ter = Decimal(str(ter_val)) if ter_val is not None else None

    dividends = (etf_data.get("dividends") or "").lower()
    accumulating = "accumulating" in dividends
    distributing = "distributing" in dividends

    return _upsert_asset(
        db,
        isin=isin,
        symbol=symbol,
        exchange="XETRA",
        name=name,
        asset_type="etf",
        currency=currency,
        country=country,
        ucits=True,
        accumulating=accumulating,
        distributing=distributing,
        ter=ter,
        provider_meta_json=json.dumps(etf_data),
    )


def _upsert_asset(
    db: Session,
    *,
    isin: str,
    symbol: str | None,
    exchange: str,
    name: str,
    asset_type: str,
    currency: str,
    country: str | None,
    ucits: bool,
    accumulating: bool | None,
    distributing: bool | None,
    ter: Decimal | None,
    provider_meta_json: str,
) -> bool:
    """Upsert an ``Asset`` by its unique (isin, exchange, currency) constraint.

    Returns ``True`` if a new row was inserted, ``False`` if an existing row
    was updated.
    """
    existing = (
        db.query(Asset)
        .filter(
            Asset.isin == isin,
            Asset.exchange == exchange,
            Asset.currency == currency,
        )
        .first()
    )

    if existing:
        existing.symbol = symbol
        existing.name = name
        existing.asset_type = asset_type
        existing.country = country
        existing.ucits = ucits
        existing.accumulating = accumulating
        existing.distributing = distributing
        existing.ter = ter
        existing.provider_meta_json = provider_meta_json
        db.flush()
        return False

    asset = Asset(
        id=uuid_pk(),
        isin=isin,
        symbol=symbol,
        exchange=exchange,
        name=name,
        asset_type=asset_type,
        currency=currency,
        country=country,
        ucits=ucits,
        accumulating=accumulating,
        distributing=distributing,
        ter=ter,
        provider_meta_json=provider_meta_json,
    )
    db.add(asset)
    db.flush()
    return True
