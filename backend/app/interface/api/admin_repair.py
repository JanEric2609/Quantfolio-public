"""Admin data-repair endpoint (Wave 2, remediation task T1.5).

Retroactively heals DKB position names/tickers and Holding asset types from
the authoritative sources built earlier in the wave:

* ``Asset`` rows — curated canonical names, symbols, and asset types.
* :func:`app.foundation.etf_classification.propagate_asset_types` — the DEC-E
  retroactive pass that re-types pre-existing Holding rows from their
  ISIN-backed Asset. Prod evidence: ``paper_holdings`` drifted to stock×12
  vs etf×3 because forward paths fixed only future creations while stale
  Holdings kept their heuristic asset_type forever.
* :func:`app.foundation.security_master.resolve_security` — optional external
  lookup (OpenFIGI ladder) for positions with no Asset evidence.

WHY this exists: DKB's FinTS wire truncates long security names into
fragments (prod receipt: ``"TERED SHS USD (ACC) O.N."`` for IE00B4L5Y983),
and historical syncs stored those fragments verbatim in ``dkb_positions``.
The forward paths were fixed in T1.x/T2.x; this endpoint repairs the
accumulated backlog once, under an explicit admin action, defaulting to a
side-effect-free dry run the operator reviews before executing with
``dry_run=false``.

Safety: admin-gated (same ``require_admin`` dependency as Discover's admin
endpoints), read-only with respect to DKB (no FinTS calls, no payment
initiation), and dry-run by default — a dry run computes the full report and
rolls the session back so the database is left byte-identical.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import Asset, DkbPosition, Holding, SecurityListing, User
from app.foundation.auth import require_admin
from app.foundation.etf_classification import propagate_asset_types
from app.foundation.portfolio.name_resolver import _is_real_name
from app.foundation.security_master import resolve_security

router = APIRouter(prefix="/api/admin/repair", tags=["admin-repair"])


class PositionIdentity(BaseModel):
    """Name/ticker pair for a position, before or after repair."""

    name: str | None = None
    ticker: str | None = None


class RepairedPosition(BaseModel):
    """Per-position repair record: what it was, what it becomes, and why."""

    isin: str | None
    account_id: str
    before: PositionIdentity
    after: PositionIdentity
    repaired_via: Literal["asset", "security_master", "skipped"]


class DkbDataRepairReport(BaseModel):
    """Structured report for the DKB data-repair run (plain JSON-serializable)."""

    dry_run: bool
    use_external_lookup: bool
    positions: list[RepairedPosition]
    positions_repaired: int
    asset_type_changes: dict[str, int]


class DkbDataRepairRequest(BaseModel):
    """Request body for POST /api/admin/repair/dkb-data."""

    dry_run: bool = True
    use_external_lookup: bool = False


def _primary_listing_symbol(db: Session, security_id: str) -> str | None:
    """Best venue symbol for a Security: primary listing first, then stable order."""
    listing = (
        db.query(SecurityListing)
        .filter(SecurityListing.security_id == security_id)
        .order_by(SecurityListing.is_primary.desc(), SecurityListing.mic.asc())
        .first()
    )
    return listing.symbol if listing is not None else None


def build_dkb_data_repair_report(
    db: Session,
    *,
    dry_run: bool,
    use_external_lookup: bool,
) -> DkbDataRepairReport:
    """Compute (and optionally apply) the full DKB data repair.

    Two passes:

    1. Name/ticker repair — every ``DkbPosition`` whose name fails
       :func:`_is_real_name` (identifier echo / truncated FinTS wire
       fragment) or whose ticker is null is repaired from the first source
       with evidence: a matching ``Asset`` row by ISIN (name + symbol as
       ticker), else — only when *use_external_lookup* is set —
       ``resolve_security`` (canonical_name + primary-listing symbol).
       An already-real name is NEVER overwritten with anything; a real-named
       position with a null ticker only gets its ticker filled.
    2. Asset-type propagation — :func:`propagate_asset_types` over all
       distinct ISINs across ``dkb_positions`` AND ``holdings``, re-typing
       stale Holding rows from authoritative Assets (DEC-E retroactive pass).

    ``dry_run=True`` computes everything but changes nothing: the report is
    built from in-memory values and the session is rolled back afterwards,
    leaving the database byte-identical. ``dry_run=False`` applies all
    mutations and commits exactly once at the end.
    """
    dkb_positions = (
        db.query(DkbPosition).order_by(DkbPosition.account_id, DkbPosition.isin).all()
    )

    entries: list[RepairedPosition] = []
    for pos in dkb_positions:
        name_is_real = _is_real_name(pos.name, pos.ticker or "", pos.isin)
        ticker_missing = not (pos.ticker or "").strip()
        if name_is_real and not ticker_missing:
            continue  # healthy position — never touched, never reported

        # Evidence ladder: curated Asset row first, security master second
        # (external lookups are opt-in — the operator decides when network
        # resolution is acceptable).
        candidate_name: str | None = None
        candidate_ticker: str | None = None
        via: Literal["asset", "security_master", "skipped"] = "skipped"
        asset = db.query(Asset).filter(Asset.isin == pos.isin).first() if pos.isin else None
        if asset is not None:
            candidate_name, candidate_ticker = asset.name, asset.symbol
            via = "asset"
        elif use_external_lookup and pos.isin:
            security = resolve_security(db, isin=pos.isin)
            if security is not None:
                candidate_name = security.canonical_name
                candidate_ticker = _primary_listing_symbol(db, security.id)
                via = "security_master"

        orig_name, orig_ticker = pos.name, pos.ticker
        new_name, new_ticker = pos.name, pos.ticker
        if via != "skipped":
            # Contract: never overwrite an already-real name with anything —
            # even when the source disagrees (curated ≠ always better for a
            # name the wire-artifact gate already accepts).
            if not name_is_real and candidate_name and _is_real_name(
                candidate_name, candidate_ticker or "", pos.isin
            ):
                new_name = candidate_name[:200]  # DkbPosition.name column width
            if ticker_missing and candidate_ticker:
                new_ticker = candidate_ticker[:32]  # DkbPosition.ticker column width

        if (new_name, new_ticker) == (orig_name, orig_ticker):
            via = "skipped"  # source found but nothing applicable
        elif not dry_run:
            pos.name = new_name
            pos.ticker = new_ticker

        entries.append(
            RepairedPosition(
                isin=pos.isin,
                account_id=pos.account_id,
                before=PositionIdentity(name=orig_name, ticker=orig_ticker),
                after=PositionIdentity(name=new_name, ticker=new_ticker),
                repaired_via=via,
            )
        )

    # DEC-E retroactive pass: re-type stale Holding rows from authoritative
    # Assets across every ISIN seen on either side (positions AND holdings),
    # so holdings whose positions were already deleted are healed too.
    isins: set[str] = {p.isin for p in dkb_positions if p.isin}
    isins.update(
        row[0]
        for row in db.query(Holding.isin).filter(Holding.isin.isnot(None)).all()
    )
    asset_type_changes = propagate_asset_types(db, sorted(isins))

    report = DkbDataRepairReport(
        dry_run=dry_run,
        use_external_lookup=use_external_lookup,
        positions=entries,
        positions_repaired=sum(1 for e in entries if e.repaired_via != "skipped"),
        asset_type_changes=asset_type_changes,
    )

    if dry_run:
        # Discard every in-session mutation (position edits, propagate's
        # Holding re-types, any alias rows resolve_security may have cached)
        # so a preview provably writes nothing.
        db.rollback()
    else:
        db.commit()  # single commit for the whole repair run
    return report


@router.post("/dkb-data", response_model=DkbDataRepairReport)
def repair_dkb_data(
    payload: DkbDataRepairRequest | None = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> DkbDataRepairReport:
    """Repair DKB position names/tickers and Holding asset types.

    Defaults to a side-effect-free dry run; execute with
    ``{"dry_run": false}`` after reviewing the report. Admin-only because it
    mutates user data across every account in one shot.
    """
    body = payload if payload is not None else DkbDataRepairRequest()
    return build_dkb_data_repair_report(
        db,
        dry_run=body.dry_run,
        use_external_lookup=body.use_external_lookup,
    )


@router.get("/dkb-data", response_model=DkbDataRepairReport)
def preview_dkb_data_repair(
    dry_run: bool = True,
    use_external_lookup: bool = False,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> DkbDataRepairReport:
    """Read-only convenience alias of POST /dkb-data.

    Always executes in dry-run mode regardless of the ``dry_run`` query flag:
    GET must never mutate state — the operator executes the repair via POST
    with ``dry_run=false``.
    """
    return build_dkb_data_repair_report(
        db,
        dry_run=True,
        use_external_lookup=use_external_lookup,
    )
