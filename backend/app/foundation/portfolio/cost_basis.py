"""Cost basis (Einstandswert) per depot position, for the rebalance tax estimate.

Resolution order for one depot position, never across depots:

1. FIFO lots booked to that depot whose open quantity matches the position
   (Scalable's ``scalable_sync`` lots, a manual opening lot, DKB debit lots);
2. the broker's own average buy price (EUR, or converted at the ECB rate);
3. unknown. FinTS does not always deliver a DKB cost, so the owner can enter
   the Einstandswert shown in the DKB app once (:func:`set_manual_cost`).

Under German tax law the acquisition cost includes the order fee, so a DKB
savings-plan debit (e.g. 251.50 EUR for a 250 EUR plan) is the cost of
the units it bought.

Tax estimates only: broker statements remain the source of truth.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.foundation.live_positions import LivePosition, live_positions
from app.foundation.models.entities import (
    ActivityLedgerEntry,
    TaxLot,
)
from app.foundation.tax_calc.jurisdictions.de.gain_harvest import OpenLot

logger = logging.getLogger(__name__)

MANUAL_SOURCE = "manual"
DEBIT_SOURCE = "dkb_debit"
_MANUAL_REF = "manual-cost:{account}:{isin}"
# Relative tolerance between the open lot quantity and the depot quantity.
LOTS_MATCH_TOLERANCE = Decimal("0.005")
# DKB's flat fee on a savings-plan order; only used to weigh how the units
# since the Einstandswert entry split across the debits (the cost is the full debit).
DKB_PLAN_FEE_EUR = Decimal("1.50")
# The few instruments whose DKB WKN is known; German ISINs carry their WKN
# (DE000<WKN>x) and are resolved without this table.
KNOWN_WKN_TO_ISIN = {"A0RPWH": "IE00B4L5Y983"}

_DEBIT_RE = re.compile(
    r"Wertp\.Abrechn\.\s+(\d{2})\.(\d{2})\.(\d{4}).*?WKN\s+([A-Z0-9]{6}).*?Gesch\.Art\s+(KV|VK)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DepotCost:
    """Cost of one depot position: total EUR (or None), where it came from, and its lots."""

    cost_eur: Decimal | None
    source: str | None  # "lots" | "lots_estimated" | "average" | None
    lots: tuple[OpenLot, ...] = ()
    reason: str | None = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _avg_cost_eur(db: Session, pos: LivePosition) -> tuple[Decimal | None, str | None]:
    if pos.avg_buy_price is None:
        return None, "no cost from the broker"
    cost = Decimal(pos.avg_buy_price) * Decimal(pos.quantity)
    ccy = (pos.currency or "EUR").upper()
    if ccy == "EUR":
        return cost, None
    from app.foundation.ecb_fx import eur_per_unit

    rate, _src = eur_per_unit(db, ccy, date.today())
    if rate is None:
        return None, f"no EUR rate for {ccy}"
    return cost * Decimal(str(rate)), None


def resolve_depot_costs(db: Session, user_id: str, positions: list[LivePosition]) -> dict[str, DepotCost]:
    """``{position id: DepotCost}`` for every position (see the module docstring for the order)."""
    lots_by_isin: dict[str, list[TaxLot]] = {}
    for lot in db.query(TaxLot).filter(TaxLot.user_id == user_id, TaxLot.closed_at.is_(None)).all():
        if lot.quantity_remaining and lot.quantity_remaining > 0:
            lots_by_isin.setdefault((lot.isin or "").upper(), []).append(lot)
    depots_per_isin: dict[str, int] = {}
    for p in positions:
        depots_per_isin[p.isin.upper()] = depots_per_isin.get(p.isin.upper(), 0) + 1

    out: dict[str, DepotCost] = {}
    for pos in positions:
        isin = pos.isin.upper()
        qty = Decimal(pos.quantity)
        lots = lots_by_isin.get(isin, [])
        if depots_per_isin[isin] > 1:
            lots = [lot for lot in lots if lot.account_ref and lot.account_ref == pos.account_id]
        else:
            lots = [lot for lot in lots if not lot.account_ref or lot.account_ref == pos.account_id]
        lot_qty = sum((Decimal(lot.quantity_remaining) for lot in lots), Decimal("0"))
        reason = None
        if lots and qty > 0 and abs(lot_qty - qty) <= qty * LOTS_MATCH_TOLERANCE:
            open_lots = tuple(
                OpenLot(lot.acquired_at, Decimal(lot.quantity_remaining), Decimal(lot.cost_basis_eur)) for lot in lots
            )
            estimated = any(lot.source == DEBIT_SOURCE for lot in lots)
            out[pos.id] = DepotCost(
                sum((lot.cost_basis_eur for lot in open_lots), Decimal("0")),
                "lots_estimated" if estimated else "lots",
                open_lots,
            )
            continue
        if lots:
            reason = "the tax lots do not add up to the depot quantity"
        avg, avg_reason = _avg_cost_eur(db, pos)
        if avg is not None:
            out[pos.id] = DepotCost(avg, "average")
        else:
            out[pos.id] = DepotCost(None, None, (), reason or avg_reason)
    return out


def fifo_gain(lots: tuple[OpenLot, ...], units_sold: Decimal, price_eur: Decimal) -> Decimal:
    """Gain (EUR, before Teilfreistellung) of selling ``units_sold`` at ``price_eur``, oldest lots first."""
    remaining = units_sold
    gain = Decimal("0")
    for lot in sorted(lots, key=lambda lot: lot.acquired_at):
        if remaining <= 0:
            break
        if lot.quantity <= 0:
            continue
        take = min(remaining, lot.quantity)
        gain += take * price_eur - lot.cost_basis_eur * (take / lot.quantity)
        remaining -= take
    return gain


# ---------------------------------------------------------------------------
# Manual Einstandswert
# ---------------------------------------------------------------------------


def _depot_position(db: Session, user_id: str, position_id: str) -> LivePosition | None:
    """The user's DKB position with this id (read through ``live_positions``)."""
    for p in live_positions(db, user_id):
        if p.id == position_id and p.source == "dkb":
            return p
    return None


def set_manual_cost(db: Session, user_id: str, position_id: str, einstandswert_eur: Decimal) -> dict | None:
    """Store the DKB-app Einstandswert (total cost of the units held now) as an opening FIFO lot.

    Replaces an earlier entry for the same position. Returns ``None`` when the
    position is not one of the user's DKB positions.
    """
    from app.foundation.tax_cockpit import _classify_holding, _safe_etf_index
    from app.foundation.tax_calc import teilfreistellung_pct_for_fund_class

    pos = _depot_position(db, user_id, position_id)
    if pos is None:
        return None
    qty = Decimal(pos.quantity or 0)
    if qty <= 0 or einstandswert_eur <= 0:
        raise ValueError("quantity and Einstandswert must be positive")
    isin = (pos.isin or "").upper()
    ref = _MANUAL_REF.format(account=pos.account_id, isin=isin)
    db.query(TaxLot).filter(TaxLot.user_id == user_id, TaxLot.source_ref == ref).delete()
    fund_class, _ = _classify_holding(user_fund_class=None, isin=isin, etf_index=_safe_etf_index())
    today = date.today()
    lot = TaxLot(
        user_id=user_id, isin=isin, symbol=pos.ticker, name=(pos.name or isin)[:200],
        account_ref=pos.account_id, fund_class=fund_class,
        teilfreistellung_pct=teilfreistellung_pct_for_fund_class(fund_class),
        acquired_at=today, quantity_initial=qty, quantity_remaining=qty,
        cost_basis_eur=einstandswert_eur, fees_eur=Decimal("0"),
        source=MANUAL_SOURCE, source_ref=ref,
        notes="Einstandswert entered by hand: one opening lot for the units held at that date.",
    )
    db.add(lot)
    db.commit()
    seed_dkb_debit_lots(db, user_id)
    return {"isin": isin, "quantity": float(qty), "cost_basis_eur": float(einstandswert_eur), "lot_id": lot.id}


def clear_manual_cost(db: Session, user_id: str, position_id: str) -> bool:
    pos = _depot_position(db, user_id, position_id)
    if pos is None:
        return False
    isin = (pos.isin or "").upper()
    ref = _MANUAL_REF.format(account=pos.account_id, isin=isin)
    db.query(TaxLot).filter(TaxLot.user_id == user_id, TaxLot.source_ref == ref).delete()
    seed_dkb_debit_lots(db, user_id)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# FIFO lots from DKB savings-plan / order debits
# ---------------------------------------------------------------------------


def parse_dkb_trade(description: str) -> tuple[date, str, str] | None:
    """``(trade date, WKN, 'KV'|'VK')`` from a DKB 'Wertp.Abrechn.' booking text, else None."""
    m = _DEBIT_RE.search(description or "")
    if not m:
        return None
    try:
        day = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return day, m.group(4).upper(), m.group(5).upper()


def _wkn_to_isin(db: Session, user_id: str) -> dict[str, str]:
    """WKN -> ISIN for the instruments held at DKB: German ISINs embed the WKN, the rest comes from the table."""
    out = dict(KNOWN_WKN_TO_ISIN)
    for p in live_positions(db, user_id):
        isin = p.isin.upper()
        if len(isin) == 12 and isin.startswith("DE000"):
            out[isin[5:11]] = isin
    return out


def seed_dkb_debit_lots(db: Session, user_id: str) -> int:
    """(Re)build FIFO lots from DKB buy debits dated after a manual opening lot. Idempotent.

    Only runs on top of a manual opening lot (which says what was held at its
    date); without one the debits cover just part of the position and say
    nothing about the rest. The cost of each lot is the exact debit
    (|amount|, fee included). The unit count is not in the booking text, so the
    units gained since the opening lot (position now minus lot quantity, exact)
    are split across the debits in proportion to amount less the 1.50 EUR plan
    fee: flagged as an estimate through ``source='dkb_debit'``. A sale (VK)
    after the opening lot makes the split unreliable, so then no lots are made.
    Returns the number of lots created.
    """
    created = 0
    wkn_map = _wkn_to_isin(db, user_id)
    dkb_positions = [p for p in live_positions(db, user_id) if p.source == "dkb"]
    manual_lots = db.query(TaxLot).filter(
        TaxLot.user_id == user_id, TaxLot.source == MANUAL_SOURCE, TaxLot.source_ref.like("manual-cost:%")
    ).all()
    ledger = (
        db.query(ActivityLedgerEntry)
        .filter(ActivityLedgerEntry.user_id == user_id, ActivityLedgerEntry.source == "dkb")
        .order_by(ActivityLedgerEntry.date)
        .all()
    )
    trades: list[tuple[ActivityLedgerEntry, date, str, str]] = []
    for entry in ledger:
        parsed = parse_dkb_trade(entry.description)
        if parsed:
            trades.append((entry, parsed[0], wkn_map.get(parsed[1], ""), parsed[2]))
    for lot_m in manual_lots:
        isin = (lot_m.isin or "").upper()
        db.query(TaxLot).filter(
            TaxLot.user_id == user_id, TaxLot.source == DEBIT_SOURCE, TaxLot.isin == isin,
            TaxLot.account_ref == lot_m.account_ref,
        ).delete()
        pos = next(
            (p for p in dkb_positions if p.account_id == lot_m.account_ref and p.isin.upper() == isin), None
        )
        # The entry day itself is ambiguous (the position may already include it).
        later = [t for t in trades if t[2] == isin and t[1] > lot_m.acquired_at]
        if pos is None or any(t[3] == "VK" for t in later):
            continue
        buys = [(e, d) for e, d, _i, kind in later if kind == "KV" and e.amount < 0]
        gained = Decimal(pos.quantity or 0) - Decimal(lot_m.quantity_initial)
        weights = [max(abs(Decimal(e.amount)) - DKB_PLAN_FEE_EUR, Decimal("0")) for e, _d in buys]
        if not buys or gained <= 0 or sum(weights) <= 0:
            continue
        total_w = sum(weights)
        for (entry, day), w in zip(buys, weights, strict=True):
            units = gained * w / total_w
            db.add(TaxLot(
                user_id=user_id, isin=isin, symbol=lot_m.symbol, name=lot_m.name,
                account_ref=lot_m.account_ref, fund_class=lot_m.fund_class,
                teilfreistellung_pct=lot_m.teilfreistellung_pct, acquired_at=day,
                quantity_initial=units, quantity_remaining=units, cost_basis_eur=abs(Decimal(entry.amount)),
                fees_eur=Decimal("0"), source=DEBIT_SOURCE, source_ref=f"dkb-debit:{entry.id}",
                notes="Cost = the DKB debit (fee included); units estimated from the position change.",
            ))
            created += 1
    db.commit()
    return created
