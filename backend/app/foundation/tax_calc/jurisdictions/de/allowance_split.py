"""Freistellungsauftrag across several banks, and the NV-Bescheinigung — pure functions.

The Sparer-Pauschbetrag is one yearly allowance (Sec. 20(9) EStG: 1,000 EUR,
2,000 EUR jointly), but each bank only knows the Freistellungsauftrag (FSA) the
customer gave *it*, and withholds Kapitalertragsteuer on every euro of capital
income above that (Sec. 44a(1), (2) no. 1 EStG). An NV-Bescheinigung from the
Finanzamt (Sec. 44a(2) no. 2 EStG) stops withholding entirely, but again only
at the banks that have a copy of it.

Rules applied here, from the BMF letter "Einzelfragen zur Abgeltungsteuer" of
14 May 2025 (IV C 1 - S 2252/00075/016/070):

* Rn. 255: an NV certificate takes precedence over an FSA at the same bank;
  when it ends, that bank's FSA applies again.
* Rn. 258/259: an FSA covers every account and depot at the bank (a Tagesgeld
  account and the depot share it); it can be raised for a year until 31 January
  of the next year and lowered only down to what the bank already used.
* Rn. 230: the bank offsets losses in its own loss pots before using the FSA.

Tax a bank withholds although another bank's FSA had room is not lost; it comes
back only through the tax return (Anlage KAP, Sec. 32d(4) EStG).

All amounts EUR Decimals. Estimates only; bank statements are the source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .allowances import KEST_RATE, SOLI_RATE, SPARER_PAUSCHBETRAG_SINGLE, SPARER_PAUSCHBETRAG_SPOUSE
from .gain_harvest import GRUNDFREIBETRAG_BY_YEAR, _by_year

ZERO = Decimal("0")
ONE_EURO = Decimal("1")


def withholding_rate(church_rate: Decimal = ZERO) -> Decimal:
    """KESt plus Soli plus church tax per euro of uncovered income.

    With church tax the KESt is reduced (Sec. 32d(1) EStG: KESt = base / (4 + k),
    the same closed form as ``reduced_kest_amount``), so the total is
    (1 + Soli + k) / (4 + k): 26.375 % without church tax, 27.82 % at 8 %,
    27.99 % at 9 %.
    """
    if church_rate <= 0:
        return KEST_RATE * (Decimal("1") + SOLI_RATE)
    return (Decimal("1") + SOLI_RATE + church_rate) / (Decimal("4") + church_rate)


@dataclass(frozen=True)
class BankYear:
    """One bank's capital income for a year, as far as the cockpit knows it."""

    bank: str
    label: str
    fsa_eur: Decimal | None  # the order on file; None = not entered
    nv_filed: bool  # a copy of the NV certificate is at this bank
    booked_eur: Decimal  # taxable income booked so far (after Teilfreistellung and the bank's loss pots)
    expected_rest_eur: Decimal  # projected taxable income for the rest of the year
    harvestable_eur: Decimal = ZERO  # unrealised taxable gain in this bank's depot


@dataclass(frozen=True)
class BankPlan:
    bank: str
    label: str
    fsa_eur: Decimal | None
    nv_filed: bool
    nv_covers: bool
    booked_eur: Decimal
    used_eur: Decimal  # FSA the bank has used up so far
    remaining_eur: Decimal  # FSA still free at this bank
    projected_eur: Decimal  # booked + expected for the whole year
    projected_uncovered_eur: Decimal  # income the current FSA/NV will not cover
    projected_withheld_eur: Decimal
    minimum_fsa_eur: Decimal  # the FSA can't go below what is already used
    recommended_fsa_eur: Decimal
    recommended_withheld_eur: Decimal

    @property
    def change_eur(self) -> Decimal:
        return self.recommended_fsa_eur - (self.fsa_eur or ZERO)


@dataclass(frozen=True)
class SplitPlan:
    allowance_eur: Decimal
    other_banks_eur: Decimal
    available_eur: Decimal  # allowance left for the banks below
    assigned_eur: Decimal  # sum of the FSAs on file at these banks
    over_assigned: bool  # FSAs on file (with other banks) exceed the allowance
    banks: tuple[BankPlan, ...]
    projected_withheld_eur: Decimal
    recommended_withheld_eur: Decimal

    @property
    def changes_needed(self) -> bool:
        return any(b.change_eur != 0 for b in self.banks)


def _floor_euro(value: Decimal) -> Decimal:
    return max(ZERO, value).quantize(ONE_EURO, rounding=ROUND_FLOOR)


def _ceil_euro(value: Decimal) -> Decimal:
    return max(ZERO, value).quantize(ONE_EURO, rounding=ROUND_CEILING)


def plan_split(
    banks: list[BankYear],
    *,
    allowance_eur: Decimal,
    other_banks_eur: Decimal = ZERO,
    nv_valid: bool = False,
    church_rate: Decimal = ZERO,
) -> SplitPlan:
    """Current coverage per bank and the FSA split that withholds the least.

    ``other_banks_eur`` is the total of the orders on file at other banks (not
    what they used): all orders together may not exceed ``allowance_eur``, so
    only the rest can go to ``banks``.

    The recommendation first keeps what each bank already used (it can't be
    taken back), then covers each bank's projected income, sharing the
    allowance in proportion when it isn't enough. A spare remainder goes to the
    bank where it can still be used: unrealised gains to harvest at a bank the
    NV certificate does not cover, else the most projected income, else (all
    covered by the NV certificate) the bank with the most income, for the day
    the certificate ends.
    """
    rate = withholding_rate(church_rate)
    available = _floor_euro(allowance_eur - other_banks_eur)
    covers = {b.bank: nv_valid and b.nv_filed for b in banks}
    projected = {b.bank: max(ZERO, b.booked_eur) + max(ZERO, b.expected_rest_eur) for b in banks}
    used = {
        b.bank: ZERO if covers[b.bank] else min(b.fsa_eur or ZERO, max(ZERO, b.booked_eur)) for b in banks
    }
    need = {b.bank: ZERO if covers[b.bank] else projected[b.bank] for b in banks}

    # Whole euros, rounded up: an order may never drop below what is already used.
    rec = {k: _ceil_euro(v) for k, v in used.items()}
    left = available - sum(rec.values(), ZERO)
    if left > 0:
        deficits = {k: max(ZERO, need[k] - rec[k]) for k in rec}
        total_deficit = sum(deficits.values(), ZERO)
        if total_deficit > 0:
            share = min(Decimal("1"), left / total_deficit)
            for k, deficit in deficits.items():
                add = _floor_euro(deficit * share) if share < 1 else deficit.quantize(ONE_EURO, rounding=ROUND_CEILING)
                add = min(add, left)
                rec[k] += add
                left -= add
        if left > 0 and banks:
            uncovered = [b for b in banks if not covers[b.bank]]
            if uncovered:
                target = max(uncovered, key=lambda b: (b.harvestable_eur, need[b.bank]))
            else:
                target = max(banks, key=lambda b: projected[b.bank])
            rec[target.bank] += left
            left = ZERO

    plans: list[BankPlan] = []
    for b in banks:
        fsa = b.fsa_eur or ZERO
        covered = covers[b.bank]
        uncovered_now = ZERO if covered else max(ZERO, projected[b.bank] - fsa)
        uncovered_rec = ZERO if covered else max(ZERO, projected[b.bank] - rec[b.bank])
        plans.append(BankPlan(
            bank=b.bank, label=b.label, fsa_eur=b.fsa_eur, nv_filed=b.nv_filed, nv_covers=covered,
            booked_eur=b.booked_eur, used_eur=used[b.bank],
            remaining_eur=ZERO if covered else max(ZERO, fsa - used[b.bank]),
            projected_eur=projected[b.bank],
            projected_uncovered_eur=uncovered_now,
            projected_withheld_eur=(uncovered_now * rate).quantize(Decimal("0.01")),
            minimum_fsa_eur=_ceil_euro(used[b.bank]),
            recommended_fsa_eur=rec[b.bank],
            recommended_withheld_eur=(uncovered_rec * rate).quantize(Decimal("0.01")),
        ))
    assigned = sum((b.fsa_eur or ZERO for b in banks), ZERO)
    return SplitPlan(
        allowance_eur=allowance_eur,
        other_banks_eur=other_banks_eur,
        available_eur=available,
        assigned_eur=assigned,
        over_assigned=assigned + other_banks_eur > allowance_eur,
        banks=tuple(plans),
        projected_withheld_eur=sum((p.projected_withheld_eur for p in plans), ZERO),
        recommended_withheld_eur=sum((p.recommended_withheld_eur for p in plans), ZERO),
    )


@dataclass(frozen=True)
class NvEligibility:
    year: int
    limit_eur: Decimal  # Grundfreibetrag + Sparer-Pauschbetrag
    grundfreibetrag_assumed: bool
    income_eur: Decimal  # other income + projected capital income
    headroom_eur: Decimal
    likely_eligible: bool


def nv_eligibility(
    year: int,
    *,
    other_income_eur: Decimal,
    capital_income_eur: Decimal,
    spouse: bool = False,
) -> NvEligibility:
    """Whether an NV-Bescheinigung is plausible for ``year`` (Sec. 44a(1) sentence 4 EStG).

    The Finanzamt issues one when no income tax would arise even with capital
    income taxed at the personal rate (Guenstigerpruefung, Sec. 32d(6) EStG):
    roughly, other income plus capital income above the Sparer-Pauschbetrag stays
    within the Grundfreibetrag. Not issued when a loss carry-forward is assessed
    or a tax return is filed on application (BMF 14.05.2025 Rn. 252).
    """
    gfb, assumed = _by_year(GRUNDFREIBETRAG_BY_YEAR, year)
    pausch = SPARER_PAUSCHBETRAG_SPOUSE if spouse else SPARER_PAUSCHBETRAG_SINGLE
    if spouse:
        gfb *= 2
    other = max(ZERO, other_income_eur)
    capital = max(ZERO, capital_income_eur)
    limit = gfb + pausch
    # The Sparer-Pauschbetrag only shelters capital income: taxable income is
    # other + (capital above the allowance), and that must stay within the
    # Grundfreibetrag. Other income alone above it rules the certificate out.
    taxable = other + max(ZERO, capital - pausch)
    headroom = gfb - other if other > gfb else limit - (other + capital)
    return NvEligibility(
        year=year, limit_eur=limit, grundfreibetrag_assumed=assumed, income_eur=other + capital,
        headroom_eur=headroom, likely_eligible=taxable <= gfb,
    )
