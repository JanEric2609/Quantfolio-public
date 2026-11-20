"""DKB dividend ingestion MVP into tax_ledger_events (audit-fixes-2026-08 todo 25).

Validation-first: the FinTS sync persists only ``{date, amount, currency,
reference}`` per transaction — no structured ISIN/WKN or withholding fields.
A transaction is ingested only when ALL of the following hold:

1. the reference matches dividend vocabulary (DIVIDENDE / ERTRAG / …);
2. a 12-character ISIN is extractable from the reference;
3. the amount is a positive credit;
4. the currency is EUR (ledger columns are ``*_eur``; FX rates are never guessed).

Everything else is skipped with a logged warning (dividend-like) or silently
ignored (ordinary credits/debits). Amounts are never fabricated: ``gross_eur``
is the booked net cash credit, withholding stays 0, and ``notes`` records that
the gross/WHT split is not derivable from FinTS statement lines.

Rows are written through the EXISTING ``tax_cockpit.create_event`` writer (the
one behind POST /api/tax/events) and deduped on ``source_ref = "dkb:<tx-id>"``
backed by the unique index from migration 0101_tax_ledger_dedupe. Fund class
comes from todo 22's evidence-based classifier via tax_cockpit's cache-only
etf_universe lookup; without evidence the conservative "other" floor applies.

Interest credits are ingested the same way: on a Tagesgeld account any credit
whose reference mentions interest (``Zins``) or the period closing
(``Abschluss``), on other accounts only explicit interest wording
(``Habenzins``, ``Zinsgutschrift``), never a line carrying an ISIN. They count
against the Freistellungsauftrag at DKB like dividends do.

Scope guard: dividends and interest only — sales/FIFO stay manual,
Vorabpauschale remains estimator-side. Every event is failure-isolated; callers additionally guard the
whole call so a sync can never fail because of ingestion.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DkbAccount, DkbTransaction, TaxLedgerEvent
from app.foundation.tax_cockpit import _classify_holding, _safe_etf_index, create_event

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORDS = ("dividende", "ertrag", "ausschuettung", "dividend", "distribution")
_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")


def is_interest_credit(reference: str | None, account_type: str | None) -> bool:
    """A credit that is interest paid by DKB (not a dividend, not a transfer)."""
    text = " ".join(str(reference or "").split()).lower()
    if not text or _ISIN_RE.search(text.upper()):
        return False
    if (account_type or "").lower() == "tagesgeld":
        return "zins" in text or "abschluss" in text
    return "habenzins" in text or "zinsgutschrift" in text


def parse_dividend_reference(reference: str | None) -> dict[str, Any] | None:
    """Extract ``{"isin": ...}`` from a dividend-like reference, else None.

    Returns None both for non-dividend references (silent skip) and for
    dividend-like references without an ISIN (caller warns). The distinction is
    made by the caller re-checking the keyword.
    """
    text = " ".join(str(reference or "").split())
    if not text:
        return None
    lowered = text.lower()
    if not any(kw in lowered for kw in _DIVIDEND_KEYWORDS):
        return None
    match = _ISIN_RE.search(text.upper())
    return {"isin": match.group(1)} if match else {"isin": None}


def ingest_dkb_dividends(db: Session, user_id: str) -> dict[str, int]:
    """Map persisted DKB dividend transactions to TaxLedgerEvent rows.

    Idempotent (source_ref dedupe), failure-isolated per event, and scoped to
    dividends only. Returns counters for sync-log visibility.
    """
    result = {
        "ingested": 0,
        "ingested_interest": 0,
        "skipped_duplicates": 0,
        "skipped_unparseable": 0,
        "skipped_non_dividend": 0,
    }
    etf_index = _safe_etf_index()
    rows = (
        db.query(DkbTransaction, DkbAccount.type)
        .join(DkbAccount, DkbTransaction.account_id == DkbAccount.id)
        .filter(DkbAccount.user_id == user_id, DkbTransaction.amount > 0)
        .all()
    )
    for tx, account_type in rows:
        try:
            if is_interest_credit(tx.reference, account_type):
                outcome = _ingest_interest(db, user_id, tx)
            else:
                outcome = _ingest_one(db, user_id, tx, etf_index)
            result[outcome] += 1
        except Exception:
            db.rollback()
            result["skipped_unparseable"] += 1
            logger.warning(
                "DKB dividend ingestion: failed on transaction %s — skipped, sync unaffected",
                tx.id,
                exc_info=True,
            )
    return result


def _ingest_one(
    db: Session, user_id: str, tx: DkbTransaction, etf_index: dict[str, dict[str, Any]]
) -> str:
    parsed = parse_dividend_reference(tx.reference)
    if parsed is None:
        return "skipped_non_dividend"
    isin = parsed["isin"]
    if not isin:
        logger.warning(
            "DKB dividend ingestion: skipping dividend-like transaction %s without "
            "parseable ISIN (reference=%r); record it manually from the broker statement",
            tx.id,
            tx.reference[:120],
        )
        return "skipped_unparseable"
    if str(tx.currency or "EUR").upper() != "EUR":
        logger.warning(
            "DKB dividend ingestion: skipping non-EUR dividend transaction %s "
            "(currency=%s); no FX rate is ever fabricated",
            tx.id,
            tx.currency,
        )
        return "skipped_unparseable"

    source_ref = f"dkb:{tx.id}"
    if db.query(TaxLedgerEvent).filter(TaxLedgerEvent.source_ref == source_ref).one_or_none():
        return "skipped_duplicates"

    fund_class, _source = _classify_holding(user_fund_class=None, isin=isin, etf_index=etf_index)
    create_event(
        db,
        user_id,
        {
            "event_type": "dividend",
            "event_date": tx.date,
            "tax_year": tx.date.year,
            "isin": isin,
            "fund_class": fund_class,
            # Booked cash credit verbatim; gross/WHT split is NOT derivable from
            # FinTS statement lines and is never invented.
            "gross_eur": Decimal(str(tx.amount)),
            "withheld_eur": Decimal("0"),
            "foreign_wht_eur": Decimal("0"),
            "confidence": "estimate",
            "source": "dkb_sync",
            "source_ref": source_ref,
            "notes": (
                "Net cash credit from DKB FinTS statement line; gross/withholding "
                "breakdown not derivable — broker Ertragsgutschrift remains the "
                "source of truth."
            ),
        },
    )
    return "ingested"


def _ingest_interest(db: Session, user_id: str, tx: DkbTransaction) -> str:
    if str(tx.currency or "EUR").upper() != "EUR":
        return "skipped_unparseable"
    source_ref = f"dkb:{tx.id}"
    if db.query(TaxLedgerEvent).filter(TaxLedgerEvent.source_ref == source_ref).one_or_none():
        return "skipped_duplicates"
    create_event(
        db,
        user_id,
        {
            "event_type": "interest",
            "event_date": tx.date,
            "tax_year": tx.date.year,
            # Booked cash credit; DKB nets any withheld tax into it.
            "gross_eur": Decimal(str(tx.amount)),
            "withheld_eur": Decimal("0"),
            "foreign_wht_eur": Decimal("0"),
            "confidence": "estimate",
            "source": "dkb_sync",
            "source_ref": source_ref,
            "institution": "dkb",
            "notes": "Interest credit from a DKB FinTS statement line; the bank's tax "
                     "statement remains the source of truth.",
        },
    )
    return "ingested_interest"
