"""Tax-free gain harvesting under a Nichtveranlagungsbescheinigung — pure functions.

While total income stays under the Grundfreibetrag, capital income up to the
Grundfreibetrag plus the Sparer-Pauschbetrag is untaxed (Sec. 32d(6) EStG,
Guenstigerpruefung; with an NV certificate under Sec. 44a(2) EStG the bank
does not even withhold). Realising gains inside that room and buying the same
units straight back raises the cost basis for free: the gain is never taxed
later. Sales are FIFO by law (Sec. 20(4) sentence 7 EStG), so a partial sale
always consumes the oldest lots first.

All amounts EUR. Estimates only; broker statements are the source of truth.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .allowances import KEST_RATE, SOLI_RATE, SPARER_PAUSCHBETRAG_SINGLE, SPARER_PAUSCHBETRAG_SPOUSE

# Sec. 32a(1) EStG. Update each year with the Basiszins (see AGENTS.md).
GRUNDFREIBETRAG_BY_YEAR: dict[int, Decimal] = {
    2023: Decimal("10908"),
    2024: Decimal("11784"),
    2025: Decimal("12096"),
    2026: Decimal("12348"),
}
# Monthly Gesamteinkommen limit of the German Familienversicherung
# (Sec. 10(1) no. 5 SGB V: one seventh of the monthly Bezugsgroesse).
# Capital income counts above the Sparer-Pauschbetrag.
FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR: dict[int, Decimal] = {
    2024: Decimal("505"),
    2025: Decimal("535"),
    2026: Decimal("565"),
}
# The tax a harvested gain would otherwise pay later: flat KESt plus Soli.
FUTURE_TAX_RATE = KEST_RATE * (Decimal("1") + SOLI_RATE)


def _by_year(table: dict[int, Decimal], year: int) -> tuple[Decimal, bool]:
    """The year's value, or the latest known one (flagged as assumed)."""
    if year in table:
        return table[year], False
    known = max((y for y in table if y <= year), default=min(table))
    return table[known], True


@dataclass(frozen=True)
class Room:
    """How much more taxable capital income the year can take tax-free."""

    year: int
    grundfreibetrag_eur: Decimal
    grundfreibetrag_assumed: bool
    sparer_pauschbetrag_eur: Decimal
    other_income_eur: Decimal
    capital_income_so_far_eur: Decimal
    tax_free_room_eur: Decimal
    # Set when the German Familienversicherung caps the room below the tax one.
    family_insurance_room_eur: Decimal | None
    room_eur: Decimal


def tax_free_room(
    year: int,
    *,
    other_income_eur: Decimal,
    capital_income_so_far_eur: Decimal,
    spouse: bool = False,
    german_family_insurance: bool = False,
) -> Room:
    """Taxable capital income (after Teilfreistellung) still untaxed this year."""
    gfb, assumed = _by_year(GRUNDFREIBETRAG_BY_YEAR, year)
    pausch = SPARER_PAUSCHBETRAG_SPOUSE if spouse else SPARER_PAUSCHBETRAG_SINGLE
    if spouse:
        gfb *= 2
    used = other_income_eur + capital_income_so_far_eur
    tax_room = max(Decimal("0"), gfb + pausch - used)
    family_room = None
    if german_family_insurance:
        monthly, _ = _by_year(FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR, year)
        family_room = max(Decimal("0"), monthly * 12 + pausch - used)
    room = tax_room if family_room is None else min(tax_room, family_room)
    return Room(
        year=year, grundfreibetrag_eur=gfb, grundfreibetrag_assumed=assumed, sparer_pauschbetrag_eur=pausch,
        other_income_eur=other_income_eur, capital_income_so_far_eur=capital_income_so_far_eur,
        tax_free_room_eur=tax_room, family_insurance_room_eur=family_room, room_eur=room,
    )


@dataclass(frozen=True)
class OpenLot:
    acquired_at: date
    quantity: Decimal
    cost_basis_eur: Decimal  # remaining basis of ``quantity``


@dataclass(frozen=True)
class Holding:
    isin: str
    name: str | None
    price_eur: Decimal
    teilfreistellung_pct: Decimal
    lots: tuple[OpenLot, ...]
    # True when ``lots`` is one average-cost stand-in for the whole position:
    # only a sale of everything is then priced correctly.
    average_cost_only: bool = False
    # A flat fee per order at this holding's broker; None uses the planner's
    # ``order_fee`` schedule.
    order_fee_eur: Decimal | None = None
    # The bank holding it ("dkb", "scalable"): each applies its own
    # Freistellungsauftrag or NV certificate, so the room can differ per bank.
    bank: str | None = None


@dataclass(frozen=True)
class HarvestStep:
    isin: str
    name: str | None
    sell_quantity: Decimal
    notional_eur: Decimal
    gain_eur: Decimal
    taxable_gain_eur: Decimal
    trading_cost_eur: Decimal
    future_tax_avoided_eur: Decimal
    whole_position: bool
    bank: str | None = None

    @property
    def net_benefit_eur(self) -> Decimal:
        return self.future_tax_avoided_eur - self.trading_cost_eur


def _fifo_sale(holding: Holding, budget: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(quantity, gain, taxable gain) of the longest FIFO sale within ``budget``."""
    keep = Decimal("1") - holding.teilfreistellung_pct
    qty = gain = taxable = Decimal("0")
    for lot in sorted(holding.lots, key=lambda lot: lot.acquired_at):
        if lot.quantity <= 0:
            continue
        lot_gain = lot.quantity * holding.price_eur - lot.cost_basis_eur
        lot_taxable = lot_gain * keep
        if taxable + lot_taxable <= budget:
            qty, gain, taxable = qty + lot.quantity, gain + lot_gain, taxable + lot_taxable
            continue
        if holding.average_cost_only or lot_taxable <= 0:
            break
        share = (budget - taxable) / lot_taxable
        qty += lot.quantity * share
        gain += lot_gain * share
        taxable = budget
        break
    return qty, gain, taxable


def plan_gain_harvest(
    holdings: list[Holding],
    room_eur: Decimal,
    order_fee: Callable[[Decimal], Decimal],
    spread_bps: Decimal,
    bank_room: dict[str, Decimal] | None = None,
) -> list[HarvestStep]:
    """Sell-and-rebuy steps that fill the room, best gain per euro traded first.

    A step is kept only when the tax it avoids later exceeds its trading
    cost (a fee and half the spread on the sale and again on the rebuy).
    ``bank_room`` caps the taxable gain realised at a bank (what its
    Freistellungsauftrag still covers when it has no NV certificate); banks
    not in it share only the overall room.
    """
    bank_left = dict(bank_room or {})
    def gain_per_euro(h: Holding) -> Decimal:
        value = sum((lot.quantity for lot in h.lots), Decimal("0")) * h.price_eur
        basis = sum((lot.cost_basis_eur for lot in h.lots), Decimal("0"))
        return (value - basis) * (Decimal("1") - h.teilfreistellung_pct) / value if value > 0 else Decimal("0")

    steps: list[HarvestStep] = []
    left = room_eur
    for holding in sorted(holdings, key=gain_per_euro, reverse=True):
        if left <= 0:
            break
        budget = left
        if holding.bank is not None and holding.bank in bank_left:
            budget = min(budget, bank_left[holding.bank])
            if budget <= 0:
                continue
        qty, gain, taxable = _fifo_sale(holding, budget)
        if qty <= 0 or taxable <= 0:
            continue
        notional = qty * holding.price_eur
        fee = holding.order_fee_eur if holding.order_fee_eur is not None else order_fee(notional)
        cost = 2 * fee + 2 * notional * spread_bps / Decimal("10000")
        step = HarvestStep(
            isin=holding.isin, name=holding.name,
            sell_quantity=qty.quantize(Decimal("0.0001")),
            notional_eur=notional.quantize(Decimal("0.01")),
            gain_eur=gain.quantize(Decimal("0.01")),
            taxable_gain_eur=taxable.quantize(Decimal("0.01")),
            trading_cost_eur=cost.quantize(Decimal("0.01")),
            future_tax_avoided_eur=(taxable * FUTURE_TAX_RATE).quantize(Decimal("0.01")),
            whole_position=qty >= sum((lot.quantity for lot in holding.lots), Decimal("0")),
            bank=holding.bank,
        )
        if step.net_benefit_eur <= 0:
            continue
        steps.append(step)
        left -= taxable
        if holding.bank is not None and holding.bank in bank_left:
            bank_left[holding.bank] -= taxable
    return steps
