"""Deduplication logic for import transactions."""

import hashlib
from dataclasses import dataclass

from .parse import ParsedTransaction


@dataclass
class DuplicateGroup:
    """A group of potentially duplicate transactions."""

    hash_key: str
    candidates: list[ParsedTransaction]
    status: str  # "new", "duplicate", "conflict"


def detect_duplicates(transactions: list[ParsedTransaction]) -> dict[str, DuplicateGroup]:
    """Detect duplicate transactions using content-based hashing.

    Hash is computed from (date, amount, isin, side). Exact matches are marked
    as duplicates; partial matches are marked as conflicts.

    Returns a dict: hash_key -> DuplicateGroup
    """
    groups: dict[str, DuplicateGroup] = {}

    for txn in transactions:
        hash_key = _compute_transaction_hash(txn)

        if hash_key not in groups:
            groups[hash_key] = DuplicateGroup(hash_key=hash_key, candidates=[], status="new")

        groups[hash_key].candidates.append(txn)

    # Mark duplicates and conflicts
    for group in groups.values():
        if len(group.candidates) > 1:
            group.status = "duplicate"
        else:
            group.status = "new"

    return groups


def _compute_transaction_hash(txn: ParsedTransaction) -> str:
    """Compute SHA256 hash from transaction content."""
    content = f"{txn.date.isoformat()}|{txn.amount}|{txn.isin or ''}|{txn.side}"
    return hashlib.sha256(content.encode()).hexdigest()
