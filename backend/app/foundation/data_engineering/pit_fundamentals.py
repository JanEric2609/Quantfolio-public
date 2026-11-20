"""Point-in-time fundamentals-by-symbol join: gvkey<->ISIN<->app symbol.

Bridges two independently point-in-time-correct sources --
:func:`app.foundation.data_engineering.fundamentals_loader.read_fundamentals`
(gvkey-keyed) and
:func:`app.foundation.data_engineering.security_identifiers_loader.read_security_identifiers`
(gvkey<->ISIN, point-in-time) -- to the app's own symbol-keyed price data, via
the security_master's ``Security``/``SecurityListing`` tables: the only place
this app already knows which ISIN a tracked symbol corresponds to.

This closes the hole ``app.lab.alphacrafter.panel`` documents in its own
docstring: fundamentals there were broadcast from a single latest snapshot
across the whole date index, because the providers registry exposes no
history. Here, for every date a caller asks about, we resolve which gvkey
(via ISIN) was that symbol's identifier as of that date, and which
fundamentals row (via ``available_from``) was public knowledge as of that
date -- both correctly time-varying, not broadcast.

**Coverage caveat.** ``SecurityListing`` is populated reactively (DKB
imports, manual resolution), not bulk-loaded from the full Compustat
universe. A symbol with no resolvable ISIN, or an ISIN outside the ingested
Compustat extract, returns ``None`` -- callers must fall back to their
existing data source. That is expected today, not a bug: coverage grows only
as more positions get resolved through security_master.

**Unit caveat**, inherited from ``fundamentals_loader``'s own currency-trap
warning: ``shares_outstanding`` (Compustat ``csho``) and the caller's price
series must be in compatible units/currency before multiplying into
``market_cap``. This module does no reconciliation -- treat ``market_cap``
magnitude as indicative until verified against the specific extract, exactly
like the provider-sourced snapshot it replaces.
"""
from __future__ import annotations

from typing import cast

import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.data_engineering.fundamentals_loader import read_fundamentals
from app.foundation.data_engineering.security_identifiers_loader import (
    read_security_identifiers,
)
from app.foundation.models.entities import Security, SecurityAlias, SecurityListing
from app.foundation.security_master import _alias_sha256

FUNDAMENTAL_VALUE_COLUMNS: tuple[str, ...] = (
    "total_assets",
    "common_equity",
    "net_income",
    "revenue",
    "shares_outstanding",
    "eps",
    "long_term_debt",
)


def resolve_isin_for_symbol(db: Session, symbol: str) -> str | None:
    """Reverse-lookup: the ISIN behind an app *symbol*, via ``SecurityListing``.

    Returns ``None`` when the symbol has never been resolved through
    security_master (see module docstring's coverage caveat) -- callers must
    treat that as "no opinion", not "confirmed absent".
    """
    if not symbol:
        return None
    row = (
        db.query(Security.isin)
        .join(SecurityListing, SecurityListing.security_id == Security.id)
        .filter(SecurityListing.symbol == symbol)
        .first()
    )
    if row is not None and row[0]:
        return str(row[0])

    # Fallback: SecurityListing.symbol may not match this app's own symbol
    # spelling for an OpenFIGI-resolved listing (e.g. "ADS" vs "ADS.DE") --
    # the SecurityAlias permanent cache is keyed by the exact alias string
    # resolve_security() was called with, so it survives that mismatch.
    alias_row = (
        db.query(Security.isin)
        .join(SecurityAlias, SecurityAlias.security_id == Security.id)
        .filter(SecurityAlias.alias_sha256 == _alias_sha256(symbol))
        .first()
    )
    return str(alias_row[0]) if alias_row is not None and alias_row[0] else None


def pit_fundamentals_for_symbol(
    db: Session, symbol: str, dates: pd.Index
) -> pd.DataFrame | None:
    """Point-in-time fundamentals for *symbol* at every timestamp in *dates*.

    Two as-of joins, chained: *dates* -> the ISIN's gvkey as of each date
    (via ``read_security_identifiers``), then that gvkey's most recently
    available fundamentals row as of each date (via ``read_fundamentals``,
    matched on ``available_from``). Both hops use ``pd.merge_asof`` with
    ``direction="backward"`` -- never a future row, exactly the guarantee
    ``fundamentals_loader``/``security_identifiers_loader`` already provide
    individually.

    Returns a DataFrame indexed by *dates* with ``gvkey``, ``currency`` and
    :data:`FUNDAMENTAL_VALUE_COLUMNS` -- NaN/None wherever coverage does not
    (yet) reach that date. Returns ``None`` outright when *symbol* has no
    resolvable ISIN, or that ISIN never appears in the ingested Compustat
    identifiers extract, signalling the caller should fall back to its
    existing data source rather than trust an all-NaN frame as "confirmed
    no fundamentals".
    """
    isin = resolve_isin_for_symbol(db, symbol)
    if isin is None:
        return None

    identifiers = read_security_identifiers()
    identifiers = cast(pd.DataFrame, identifiers[identifiers["isin"] == isin])
    if identifiers.empty:
        return None
    identifiers = identifiers.sort_values("as_of_date")[["as_of_date", "gvkey"]]

    # security_identifiers_loader/fundamentals_loader store tz-naive
    # timestamps; a caller's index (e.g. BarStore bars) may be tz-aware.
    # merge_asof silently matches nothing across that mismatch rather than
    # raising, so join on a naive working column and carry the caller's
    # original (possibly tz-aware, possibly unsorted) timestamps through
    # untouched in ``orig_date`` to restore as the final index.
    original_index = pd.DatetimeIndex(dates)
    naive_dates = (
        original_index.tz_localize(None) if original_index.tz is not None else original_index
    )
    left = pd.DataFrame({"orig_date": original_index, "date": naive_dates}).sort_values("date")

    linked = pd.merge_asof(
        left, identifiers, left_on="date", right_on="as_of_date", direction="backward"
    )

    gvkeys = sorted({g for g in linked["gvkey"].dropna().unique().tolist()})
    if not gvkeys:
        return None

    fundamentals = read_fundamentals()
    fundamentals = cast(pd.DataFrame, fundamentals[fundamentals["gvkey"].isin(gvkeys)])
    if fundamentals.empty:
        return None
    fundamentals = fundamentals.sort_values("available_from")

    columns = ["gvkey", "available_from", "currency", *FUNDAMENTAL_VALUE_COLUMNS]
    joined = pd.merge_asof(
        linked.sort_values("date"),
        fundamentals[columns],
        left_on="date",
        right_on="available_from",
        by="gvkey",
        direction="backward",
    )
    joined = joined.set_index("orig_date").reindex(original_index)
    return cast(pd.DataFrame, joined[["gvkey", "currency", *FUNDAMENTAL_VALUE_COLUMNS]])
