"""The currency a symbol's stored prices are quoted in.

``bar_prices.currency`` and ``price_cache.currency`` used to be filled from
the ``assets`` table, which holds a fund's base currency (EUNL.DE read USD
although Xetra quotes it in EUR), or from an "EUR" fallback (AAPL, MU,
NESN.SW and SHEL.L all read EUR). On 2026-09-28, 429 of the 780 symbols with
bars were mislabelled. The prices themselves were native: SHEL.L in pence,
NOVO-B.CO in kroner.

Readers resolve the quote unit here, in this order:

1. ``listing_currencies``, the currency the provider reports for the series
   it returns. yfinance puts it in every history response's metadata, so
   ingestion records it for free, and :func:`audit_listing_currencies`
   (at worker start, then daily) fills in the symbols ingested before this
   table existed.
2. A label the caller already holds from a live quote, when it has one.
3. The listing-suffix rule (``providers.utils.listing_quote_currency``). It
   matched yfinance for 765 of the 780 symbols; it cannot know that the LSE
   quotes IWDA.L, CSPX.L, IHG.L and CPG.L in USD, or that yfinance serves
   some Xetra ETF lines (SXRM.DE) in USD.

Codes keep the provider's case: "GBp" is pence, "GBP" pounds
(``providers.utils.currency_unit``).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.foundation.models.entities import ListingCurrency
from app.foundation.providers.utils import currency_unit, listing_quote_currency

logger = logging.getLogger(__name__)

# A stored provider answer is re-checked after this long (listings do change
# quote currency: IHG.L and CPG.L moved to USD).
RECHECK_AFTER = timedelta(days=30)

_ISO_LEN = 3

_RELABEL_SQL = (
    ("bar_prices", text("UPDATE bar_prices SET currency = :c WHERE symbol = :s AND currency <> :c")),
    ("price_cache", text("UPDATE price_cache SET currency = :c WHERE ticker = :s AND currency <> :c")),
)


def _valid(code: str | None) -> bool:
    return bool(code) and len(str(code).strip()) == _ISO_LEN


def resolve_quote_currency(db: Session | None, symbol: str, *, hint: str | None = None) -> str:
    """The unit *symbol*'s stored prices are quoted in (may be a minor unit)."""
    sym = symbol.strip().upper()
    if db is not None:
        try:
            row = db.get(ListingCurrency, sym)
        except Exception:  # noqa: BLE001 - table missing before its migration
            row = None
        if row is not None and _valid(row.currency):
            return row.currency
    if _valid(hint):
        return str(hint).strip()
    return listing_quote_currency(sym)


def resolve_currency(db: Session | None, symbol: str, *, hint: str | None = None) -> str:
    """ISO currency of *symbol*'s prices (pence -> GBP); what FX lookups need."""
    return currency_unit(resolve_quote_currency(db, symbol, hint=hint))[0]


def record_listing_currency(db: Session, symbol: str, currency: str | None, source: str) -> bool:
    """Upsert a provider-reported quote currency. Returns True when stored.

    Does not commit; the caller owns the transaction.
    """
    if not _valid(currency):
        return False
    sym = symbol.strip().upper()
    code = str(currency).strip()
    # Pending rows first: with autoflush off, db.get() does not see a row
    # added earlier in this transaction (market.history caches a price per
    # bar, each carrying the same currency).
    row = next(
        (o for o in db.new if isinstance(o, ListingCurrency) and o.symbol == sym), None
    ) or db.get(ListingCurrency, sym)
    now = datetime.now(UTC)
    if row is None:
        db.add(ListingCurrency(symbol=sym, currency=code, source=source, checked_at=now))
    else:
        if row.currency != code:
            logger.info("listing_currency: %s %s -> %s (%s)", sym, row.currency, code, source)
        row.currency, row.source, row.checked_at = code, source, now
    return True


def relabel_stored_prices(db: Session, symbol: str, currency: str) -> int:
    """Set every stored bar and cached price of *symbol* to *currency*.

    Returns the number of rows changed. Neither commits nor rolls back: the
    caller owns the transaction, and ``ingest_bar_prices`` calls this between
    inserting its bars and committing them, so a rollback here would drop
    those bars while ingestion still reported success. A database error
    propagates to the caller instead.
    """
    sym = symbol.strip().upper()
    inspector = inspect(db.connection())
    changed = 0
    for table, statement in _RELABEL_SQL:
        # bar_prices is created by migrations, not by the ORM metadata, so
        # databases built with create_all() (most tests) do not have it.
        if not inspector.has_table(table):
            continue
        result = db.execute(statement, {"c": currency, "s": sym})
        changed += int(getattr(result, "rowcount", 0) or 0)
    return changed


def _stored_symbols(db: Session) -> list[str]:
    symbols: set[str] = set()
    for sql in ("SELECT DISTINCT symbol FROM bar_prices", "SELECT DISTINCT ticker FROM price_cache"):
        try:
            symbols.update(str(r[0]).upper() for r in db.execute(text(sql)) if r[0])
        except Exception:  # noqa: BLE001
            db.rollback()
    return sorted(symbols)


def _yfinance_currency(db: Session) -> Callable[[str], str | None]:
    """A probe that reads the quote currency from a short yfinance history."""
    from app.foundation.providers.registry import build_provider_registry
    from app.foundation.providers.yfinance_provider import YFinanceProvider

    provider = next(
        (p for p in build_provider_registry(db).providers if isinstance(p, YFinanceProvider)), None
    )

    def probe(symbol: str) -> str | None:
        if provider is None:
            return None
        result = provider.get_history(symbol, days=5)
        rows = result.get("data") if result.get("ok") else None
        if not rows:
            return None
        return rows[-1].get("currency")

    return probe


def audit_listing_currencies(
    db: Session,
    *,
    fetch: Callable[[str], str | None] | None = None,
    symbols: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Record each stored symbol's provider-reported currency and relabel its rows.

    Symbols checked within :data:`RECHECK_AFTER` are skipped, so the daily
    run only probes new symbols and month-old answers. A symbol the provider
    does not answer for keeps the suffix rule, and its rows are relabelled to
    that rule: any label is better than the old "EUR" default. Each symbol
    commits on its own; a database error rolls back that symbol only.
    """
    probe = fetch or _yfinance_currency(db)
    clock = now or datetime.now(UTC)
    counts = {
        "checked": 0, "skipped_recent": 0, "provider": 0, "suffix_rule": 0, "rows_relabelled": 0, "failed": 0,
    }
    for sym in symbols if symbols is not None else _stored_symbols(db):
        sym = sym.strip().upper()
        row = db.get(ListingCurrency, sym)
        checked_at = row.checked_at if row is not None else None
        if checked_at is not None and checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        if checked_at is not None and clock - checked_at < RECHECK_AFTER:
            counts["skipped_recent"] += 1
            continue
        counts["checked"] += 1
        try:
            reported = probe(sym)
        except Exception:  # noqa: BLE001 - one bad symbol must not stop the audit
            logger.debug("listing_currency: probe failed for %s", sym, exc_info=True)
            reported = None
        try:
            if record_listing_currency(db, sym, reported, "yfinance_metadata"):
                source, code = "provider", str(reported).strip()
            else:
                source, code = "suffix_rule", resolve_quote_currency(db, sym)
            relabelled = relabel_stored_prices(db, sym, code)
            db.commit()
        except Exception:  # noqa: BLE001 - one bad symbol must not stop the audit
            db.rollback()
            counts["failed"] += 1
            logger.warning("listing_currency: audit failed for %s", sym, exc_info=True)
            continue
        counts[source] += 1
        counts["rows_relabelled"] += relabelled
    logger.info("listing_currency audit: %s", counts)
    return counts
