"""Commit parsed transactions to the database."""

import hashlib
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.foundation.models.entities import ActivityLedgerEntry

from .parse import ParsedTransaction


def _decimal(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value else None


def commit_import(
    db: Session,
    user_id: str,
    transactions: list[ParsedTransaction],
    source: str = "csv_import",
) -> tuple[list[str], list[str]]:
    """Commit parsed transactions to ActivityLedgerEntry.

    Quantity, unit price and fees go onto the entry too: they are what
    ``tax_cockpit.seed_lots_from_activity`` turns a ledger "buy" into a tax lot
    with (cost = quantity * price + fees). Entries stay ``review_state="pending"``
    and lots are seeded by the user's explicit ``/api/tax/lots/seed-from-activity``.

    A row already imported without a quantity (an earlier import of the same
    file, before quantity/price were read) is completed in place instead of
    being rejected as a duplicate, so re-uploading the file enables its lots.

    Returns:
        (list of created entry IDs, list of errors/skipped)
    """
    created_ids = []
    errors = []

    for txn in transactions:
        try:
            # Create dedupe hash
            hash_key = hashlib.sha256(
                f"{txn.date.isoformat()}|{txn.amount}|{txn.isin or ''}|{txn.side}".encode()
            ).hexdigest()

            # Check for existing
            existing = db.query(ActivityLedgerEntry).filter(
                ActivityLedgerEntry.user_id == user_id,
                ActivityLedgerEntry.dedupe_hash == hash_key,
                ActivityLedgerEntry.source == source,
            ).first()

            if existing:
                if existing.quantity is None and txn.quantity:
                    existing.quantity = _decimal(txn.quantity)
                    existing.price = _decimal(txn.price)
                    existing.fees = _decimal(txn.fees)
                    errors.append(f"Updated quantity/price of existing entry: {txn.date} {txn.description}")
                else:
                    errors.append(f"Duplicate: {txn.date} {txn.description}")
                continue

            # Create entry
            entry = ActivityLedgerEntry(
                id=str(uuid4()),
                user_id=user_id,
                source=source,
                dedupe_hash=hash_key,
                # "buy"/"sell", the ledger's own vocabulary: tax-lot seeding reads
                # "buy", and the amount below is unsigned, so the side lives here.
                activity_type=txn.side if txn.isin or txn.ticker else "cashflow",
                date=txn.date.date(),
                amount=Decimal(str(txn.amount)),
                currency=txn.currency,
                description=txn.description,
                isin=txn.isin,
                symbol=txn.ticker,
                quantity=_decimal(txn.quantity),
                price=_decimal(txn.price),
                fees=_decimal(txn.fees),
                review_state="pending",  # Require user review for imports
            )

            db.add(entry)
            # Flush so a second identical row in the same file is seen as a
            # duplicate below instead of violating the unique index at commit.
            db.flush()
            created_ids.append(entry.id)

        except Exception as e:
            errors.append(f"Failed to create entry: {str(e)}")

    db.commit()
    return created_ids, errors
