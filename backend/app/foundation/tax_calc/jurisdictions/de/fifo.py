"""FIFO lot consumption for capital-gain estimates."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class FifoSale:
    proceeds_eur: Decimal
    realised_gain_eur: Decimal
    quantity_sold: Decimal
    cost_basis_eur: Decimal
    consumed_lots: list[dict] = field(default_factory=list)


@dataclass
class _Lot:
    lot_id: str
    acquired_at: date
    quantity_remaining: Decimal
    cost_basis_eur: Decimal      # total remaining cost basis (proportional to quantity_remaining)
    quantity_initial: Decimal
    cost_basis_initial: Decimal


def fifo_consume(
    lots: list[dict],
    sell_quantity: Decimal,
    sell_price_per_unit_eur: Decimal,
    sell_fees_eur: Decimal = Decimal("0"),
) -> FifoSale:
    """Consume the given lots FIFO and compute realised gain.

    ``lots`` is a list of dicts with keys ``lot_id``, ``acquired_at`` (date or ISO str),
    ``quantity_remaining`` (Decimal-like), ``cost_basis_eur`` (remaining basis, Decimal-like),
    optionally ``quantity_initial`` / ``cost_basis_initial`` (defaults to the remaining values).

    The function mutates lot dicts in place: ``quantity_remaining`` and ``cost_basis_eur``
    are decremented. The caller is responsible for persistence.
    """
    if sell_quantity <= 0:
        raise ValueError("sell_quantity must be positive")

    proceeds = (sell_price_per_unit_eur * sell_quantity).quantize(Decimal("0.000001")) - sell_fees_eur
    remaining_to_sell = sell_quantity
    total_basis = Decimal("0")
    consumed: list[dict] = []

    # Sort by acquired_at ASC.
    sorted_lots = sorted(
        lots,
        key=lambda lot: _as_date(lot["acquired_at"]),
    )

    for lot in sorted_lots:
        if remaining_to_sell <= 0:
            break
        lot_qty = Decimal(str(lot["quantity_remaining"]))
        if lot_qty <= 0:
            continue
        take = min(lot_qty, remaining_to_sell)
        lot_basis_remaining = Decimal(str(lot["cost_basis_eur"]))
        # Proportional basis for the consumed slice.
        slice_basis = (lot_basis_remaining * take / lot_qty) if lot_qty > 0 else Decimal("0")
        total_basis += slice_basis

        lot["quantity_remaining"] = lot_qty - take
        lot["cost_basis_eur"] = lot_basis_remaining - slice_basis
        consumed.append(
            {
                "lot_id": lot.get("lot_id"),
                "acquired_at": str(lot["acquired_at"]),
                "quantity_taken": str(take),
                "cost_basis_consumed_eur": str(slice_basis.quantize(Decimal("0.01"))),
            }
        )
        remaining_to_sell -= take

    if remaining_to_sell > 0:
        raise ValueError(
            f"Insufficient lots to satisfy sale: {remaining_to_sell} units short."
        )

    realised = (proceeds - total_basis).quantize(Decimal("0.01"))
    return FifoSale(
        proceeds_eur=proceeds.quantize(Decimal("0.01")),
        realised_gain_eur=realised,
        quantity_sold=sell_quantity,
        cost_basis_eur=total_basis.quantize(Decimal("0.01")),
        consumed_lots=consumed,
    )


def _as_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
