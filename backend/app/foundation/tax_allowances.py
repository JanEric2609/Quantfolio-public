"""Freistellungsauftrag and NV-Bescheinigung across DKB and Scalable Capital.

Each bank withholds Kapitalertragsteuer on capital income above the
Freistellungsauftrag (FSA) the customer gave *that* bank, unless it holds a
copy of a valid NV-Bescheinigung (Sec. 44a EStG). This module works out, per
bank and year, what is booked, what the rest of the year will probably bring
(Tagesgeld interest, dividends, January's Vorabpauschale), how much tax the
current orders would let the bank withhold, and the split that withholds the
least, plus the concrete steps and deadlines at each bank.

The split itself is the pure ``tax_calc.jurisdictions.de.allowance_split``;
this module only gathers the inputs. Estimates only, not tax advice; the
banks' tax statements are the source of truth.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.models.entities import AppSetting, ConnectedAccount, DkbAccount, TaxLedgerEvent
from app.foundation.tax_calc.jurisdictions.de.allowance_split import (
    BankYear,
    SplitPlan,
    nv_eligibility,
    plan_split,
    withholding_rate,
)
from app.foundation.tax_calc.jurisdictions.de.vorabpauschale import first_working_day
from app.foundation.tax_calc.jurisdictions.de.allowances import (
    SPARER_PAUSCHBETRAG_SINGLE,
    SPARER_PAUSCHBETRAG_SPOUSE,
)
from app.foundation.settings import get_public_settings, get_setting_row, invalidate_settings_cache
from app.foundation.tax_calc import apply_teilfreistellung, plan_gain_harvest, tax_free_room
from app.foundation.tax_cockpit import (
    ESTIMATE_DISCLAIMER,
    HARVEST_SPREAD_BPS,
    _church_rate,
    _estimate_holdings_vorabpauschale,
    _estimation_allowed,
    _harvest_holdings,
    _harvest_order_fee,
    _nv_warnings,
    compute_year_overview,
    event_institution,
    get_user_jurisdiction,
    list_events,
)

ZERO = Decimal("0")
BANKS: tuple[tuple[str, str], ...] = (("dkb", "DKB"), ("scalable", "Scalable Capital"))
# The per-bank orders and NV copies describe one person's accounts, but settings
# are installation-wide: they belong to the user who set them, and only that
# user's plan reads them (another user sees them as not entered).
BANK_SETTING_KEYS = (
    "tax_nv_certificate",
    "tax_nv_valid_until",
    "freistellungsauftrag_dkb_eur",
    "freistellungsauftrag_scalable_eur",
    "freistellungsauftrag_other_banks_eur",
    "tax_nv_filed_dkb",
    "tax_nv_filed_scalable",
    "tax_interest_rate_dkb",
)
BANK_SETTINGS_OWNER_KEY = "tax_bank_settings_user_id"
# Synced cash credits are booked net: above the Freistellungsauftrag the bank
# has already taken its tax off them.
_NET_CREDIT_SOURCES = frozenset({"dkb_sync", "scalable_sync"})
BANK_LABELS: dict[str, str] = dict(BANKS)
# Health-insurance settings whose income limit caps the harvest room: German
# family insurance (Sec. 10 SGB V), and co-insurance abroad, where EU rules may
# apply the same test to a family member living in Germany (conservative).
FAMILY_LIMIT_INSURANCE = frozenset({"de_family", "foreign"})

# Deadlines the banks and the law set (see docs/tax-cockpit.md for sources).
FSA_CHANGE_DEADLINE = (12, 31)  # banks accept changes for the current year until 31 Dec
NV_SCALABLE_DEADLINE = (12, 15)  # Scalable: the copy must arrive by 15 Dec
VERLUSTBESCHEINIGUNG_DEADLINE = (12, 15)  # Sec. 43a(3) sentence 5 EStG

HOW_TO: dict[str, dict[str, str]] = {
    "dkb": {
        "fsa": "DKB Banking (web or app): Mein Profil › Steuerliche Angaben › Freistellungsauftrag, change the "
               "amount and confirm. One order covers the Girokonto, the Tagesgeld and the depot. Changes work for "
               "the current or the next year, never for a past one.",
        "nv": "Upload the NV certificate as a file in DKB Banking (Mein Profil › Steuerliche Angaben); once "
              "accepted it is listed under Freistellungsaufträge.",
        "loss": "Ask DKB for a Verlustbescheinigung (Sec. 43a(3) EStG) in Banking or through the service; the "
                "request must reach DKB by 15 December and cannot be withdrawn.",
    },
    "scalable": {
        "fsa": "Scalable app: Profil › Freistellungsauftrag (web: Profil › Steuern). One order covers the depot and "
               "the Tagesgeld (overnight) account. Changes for the current year are possible until 31 December.",
        "nv": "Send Scalable a copy (never the original): ask support in the app or through the contact form for "
              "a personal upload link, or post it to Scalable Capital Bank GmbH, Seitzstraße 8e, 80538 München. "
              "It must arrive by 15 December to count for this year.",
        "loss": "Ask Scalable support for a Verlustbescheinigung (Sec. 43a(3) EStG); the request must reach them by "
                "15 December and cannot be withdrawn.",
    },
}

SOURCES: list[dict[str, str]] = [
    {"label": "§ 20 Abs. 9 EStG (Sparer-Pauschbetrag)", "url": "https://www.gesetze-im-internet.de/estg/__20.html"},
    {"label": "§ 44a EStG (Freistellungsauftrag, NV-Bescheinigung)",
     "url": "https://www.gesetze-im-internet.de/estg/__44a.html"},
    {"label": "§ 43a Abs. 3 EStG (Verlustbescheinigung)", "url": "https://www.gesetze-im-internet.de/estg/__43a.html"},
    {"label": "§ 32d Abs. 4 EStG (Anlage KAP)", "url": "https://www.gesetze-im-internet.de/estg/__32d.html"},
    {"label": "§ 18 Abs. 3 InvStG (Vorabpauschale in January)",
     "url": "https://www.gesetze-im-internet.de/invstg_2018/__18.html"},
    {"label": "BMF, Einzelfragen zur Abgeltungsteuer, 14.05.2025 (Rn. 230, 252–260)",
     "url": "https://www.bundesfinanzministerium.de/Content/DE/Downloads/BMF_Schreiben/Steuerarten/Abgeltungsteuer/"
            "2025-05-14-einzelfragen-zur-abgeltungsteuer.pdf"},
    {"label": "Scalable: Freistellungsauftrag also covers the Tagesgeld",
     "url": "https://help.scalable.capital/tagesgeld-fdf4fe44/zinsen-konditionen-5bbf4b40/"
            "wird-mein-freistellungsauftrag-auch-f%C3%BCr-das-tagesgeldkon-87c1de18"},
    {"label": "Scalable: NV-Bescheinigung", "url": "https://help.scalable.capital/steuern-de12f868/"
     "was-ist-eine-nichtveranlagungsbescheinigung-473d7d7b"},
    {"label": "DKB: Freistellungsauftrag", "url": "https://www.dkb.de/fragen-antworten/wie-hinterlege-ich-einen-freistellungsauftrag"},
]


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _rate(value: Any) -> Decimal | None:
    """An interest rate as a fraction; values above 1 are read as percent."""
    rate = _dec(value)
    if rate is None or rate < 0:
        return None
    return rate / Decimal("100") if rate > 1 else rate


@dataclass
class _Pots:
    dividends: Decimal = ZERO
    interest: Decimal = ZERO
    vorab: Decimal = ZERO
    gain_aktien: Decimal = ZERO
    loss_aktien: Decimal = ZERO
    gain_other: Decimal = ZERO
    loss_other: Decimal = ZERO
    net_credits: Decimal = ZERO  # synced dividends and interest, booked after any withholding
    events: list[TaxLedgerEvent] = field(default_factory=list)

    def add(self, ev: TaxLedgerEvent) -> None:
        self.events.append(ev)
        gross = Decimal(str(ev.gross_eur or 0))
        tf = Decimal(str(ev.teilfreistellung_pct or 0))
        net = ev.source in _NET_CREDIT_SOURCES and not Decimal(str(ev.withheld_eur or 0))
        if ev.event_type == "dividend":
            self.dividends += apply_teilfreistellung(gross, tf)
            if net:
                self.net_credits += apply_teilfreistellung(gross, tf)
        elif ev.event_type == "interest":
            self.interest += gross
            if net:
                self.net_credits += gross
        elif ev.event_type == "vorabpauschale":
            self.vorab += apply_teilfreistellung(gross, tf)
        elif ev.event_type == "sale":
            realised = apply_teilfreistellung(Decimal(str(ev.realised_gain_eur or 0)), tf)
            if (ev.bucket or "sonstige").lower() == "aktien":
                if realised >= 0:
                    self.gain_aktien += realised
                else:
                    self.loss_aktien -= realised
            elif realised >= 0:
                self.gain_other += realised
            else:
                self.loss_other -= realised

    def taxable(self) -> tuple[Decimal, Decimal, Decimal]:
        """(taxable income, Aktien loss left, other loss left) after this bank's own loss pots."""
        net_aktien = max(ZERO, self.gain_aktien - self.loss_aktien)
        aktien_left = max(ZERO, self.loss_aktien - self.gain_aktien)
        income = self.dividends + self.interest + self.vorab + net_aktien + self.gain_other
        offset = min(self.loss_other, income)
        return income - offset, aktien_left, self.loss_other - offset


def _net_credits_above(events: list[TaxLedgerEvent], fsa: Decimal, opening_vorab: Decimal) -> Decimal:
    """The part of the net-booked credits that arrived after the order was used up.

    The bank uses the order in booking order, so a dividend credited while the
    order still had room was paid gross, even if a later gain used the order up.
    Replays the bank's pots by date (January's Vorabpauschale first).
    """
    running = _Pots(vorab=opening_vorab)
    above = ZERO
    for ev in sorted(events, key=lambda e: e.event_date):
        before, credits_before = running.taxable()[0], running.net_credits
        running.add(ev)
        credit = running.net_credits - credits_before
        if credit > 0:
            uncovered = max(ZERO, running.taxable()[0] - fsa) - max(ZERO, before - fsa)
            above += min(credit, max(ZERO, uncovered))
    return above


def _bank_of(ev: TaxLedgerEvent) -> str | None:
    return event_institution(ev)


def _year_fractions(year: int, today: date) -> tuple[Decimal, Decimal]:
    """(share of the year already past, share still ahead)."""
    if year < today.year:
        return Decimal("1"), ZERO
    if year > today.year:
        return ZERO, Decimal("1")
    days = Decimal((date(year, 12, 31) - date(year, 1, 1)).days + 1)
    past = Decimal((today - date(year, 1, 1)).days + 1) / days
    return past, Decimal("1") - past


def _tagesgeld(db: Session, user_id: str, settings: dict[str, Any]) -> dict[str, tuple[Decimal, Decimal | None]]:
    """Per bank: (savings balance, interest rate or None)."""
    dkb_balance = sum(
        (Decimal(str(a.balance or 0)) for a in db.query(DkbAccount).filter(
            DkbAccount.user_id == user_id, DkbAccount.type == "tagesgeld")),
        ZERO,
    )
    out = {"dkb": (dkb_balance, _rate(settings.get("tax_interest_rate_dkb")))}
    sc_balance = ZERO
    sc_rate = None
    for acc in db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user_id, ConnectedAccount.source == "scalable"
    ):
        if acc.account_type == "savings":
            sc_balance += Decimal(str(acc.balance or 0))
        try:
            raw = json.loads(acc.raw_json or "{}")
        except ValueError:
            raw = {}
        overnight = raw.get("overnight") if isinstance(raw, dict) else None
        if isinstance(overnight, dict) and sc_rate is None:
            sc_rate = _rate(overnight.get("interest_rate"))
    out["scalable"] = (sc_balance, sc_rate)
    return out


def _vorab_by_bank(
    db: Session, user_id: str, year: int, booked: dict[str | None, set[str]],
) -> tuple[dict[str, Decimal], Decimal]:
    """January's Vorabpauschale (for ``year - 1``) by the bank holding each fund, estimated.

    ``booked`` maps a bank (None: no bank recorded) to the ISINs whose
    Vorabpauschale is already a booked event there. Those are left out at that
    bank only (an event without a bank covers the fund everywhere), so a fund
    sold since January isn't counted twice and the same fund at the other bank
    still is.
    """
    from app.foundation.live_positions import live_positions

    items, _total = _estimate_holdings_vorabpauschale(db, user_id, year - 1)
    anywhere = set().union(*booked.values()) if booked else set()
    nowhere_known = booked.get(None, set())
    qty: dict[str, dict[str, Decimal]] = {}
    for pos in live_positions(db, user_id):
        qty.setdefault(pos.isin.upper(), {}).setdefault(pos.source, ZERO)
        qty[pos.isin.upper()][pos.source] += Decimal(pos.quantity or 0)
    by_bank: dict[str, Decimal] = {}
    unassigned = ZERO
    for item in items:
        isin = str(item.get("isin") or "").upper()
        taxable = Decimal(str(item.get("taxable_after_teilfreistellung_eur") or 0))
        held = qty.get(isin, {})
        total = sum(held.values(), ZERO)
        if total <= 0:
            if isin not in anywhere:
                unassigned += taxable
            continue
        if isin in nowhere_known:
            continue
        for bank, q in held.items():
            if isin not in booked.get(bank, set()):
                by_bank[bank] = by_bank.get(bank, ZERO) + taxable * q / total
    return by_bank, unassigned


def _harvestable_by_bank(db: Session, user_id: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    try:
        holdings, _skipped, _warnings = _harvest_holdings(db, user_id)
    except Exception:  # pragma: no cover - market/universe lookups are best effort
        return out
    for h in holdings:
        value = sum((lot.quantity for lot in h.lots), ZERO) * h.price_eur
        basis = sum((lot.cost_basis_eur for lot in h.lots), ZERO)
        gain = (value - basis) * (Decimal("1") - h.teilfreistellung_pct)
        if gain > 0 and h.bank:
            out[h.bank] = out.get(h.bank, ZERO) + gain
    return out


def nv_status(settings: dict[str, Any], year: int) -> dict[str, Any]:
    """Whether an NV certificate covers ``year`` and at which banks a copy is filed.

    A certificate always ends on 31 December (Sec. 44a(2) sentence 3 EStG). One
    that ends earlier in ``year`` was revoked or mistyped; it is treated as not
    covering the year, so the plan errs toward withholding.
    """
    has = bool(settings.get("tax_nv_certificate"))
    valid_until = None
    raw = settings.get("tax_nv_valid_until")
    if raw:
        try:
            valid_until = date.fromisoformat(str(raw)[:10])
        except ValueError:
            valid_until = None
    year_end = date(year, 12, 31)
    valid = has and (valid_until is None or valid_until >= year_end)
    ends_midyear = bool(has and valid_until and date(year, 1, 1) <= valid_until < year_end)
    flags = {bank: settings.get(f"tax_nv_filed_{bank}") for bank, _ in BANKS}
    # None = never answered (older settings had only "NV certificate filed");
    # an explicit False means that bank has no copy.
    banks_known = any(value is not None for value in flags.values())
    filed = {bank: bool(value) for bank, value in flags.items()}
    return {
        "has_certificate": has,
        "valid_for_year": valid,
        "valid_until": valid_until.isoformat() if valid_until else None,
        "ends_this_year": bool(valid and valid_until and valid_until.year == year),
        "ends_midyear": ends_midyear,
        "filed_at": [bank for bank, on in filed.items() if on],
        "banks_known": banks_known,
        # Unanswered: every bank is assumed to have a copy, and the plan asks.
        "filed": filed if banks_known else {bank: has for bank, _ in BANKS},
    }


def settings_for_user(db: Session, user_id: str) -> dict[str, Any]:
    """Public settings, with the per-bank keys blanked unless ``user_id`` set them."""
    settings = get_public_settings(db)
    owner = settings.get(BANK_SETTINGS_OWNER_KEY)
    if owner and owner != user_id:
        settings = {**settings, **{key: None for key in BANK_SETTING_KEYS}, "_bank_settings_other_user": True}
        settings["tax_nv_certificate"] = False
    return settings


class BankSettingsOwnedByOtherUser(Exception):
    """The per-bank Freistellungsauftrag/NV settings belong to another account."""


def claim_bank_settings(db: Session, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Check (and record) who owns the per-bank settings before ``changes`` are saved.

    Returns ``changes`` plus the owner key when a per-bank value changes. Raises
    ``BankSettingsOwnedByOtherUser`` when another user already owns them.
    """
    current = get_public_settings(db)
    touched = [key for key in BANK_SETTING_KEYS if key in changes and changes[key] != current.get(key)]
    if not touched:
        return changes
    owner = current.get(BANK_SETTINGS_OWNER_KEY)
    if not owner:
        # Claim it with one INSERT: the key is unique, so of two first writers
        # racing here exactly one wins and the other is told who owns it.
        db.add(AppSetting(key=BANK_SETTINGS_OWNER_KEY, value_json=json.dumps(user_id)))
        try:
            db.commit()
            owner = user_id
        except IntegrityError:
            db.rollback()
            owner = get_setting_row(db, BANK_SETTINGS_OWNER_KEY)
        finally:
            invalidate_settings_cache()
    if owner != user_id:
        raise BankSettingsOwnedByOtherUser(
            "These Freistellungsauftrag and NV settings belong to another account on this installation."
        )
    return changes


def _fsa_setting(settings: dict[str, Any], bank: str) -> Decimal | None:
    value = _dec(settings.get(f"freistellungsauftrag_{bank}_eur"))
    return max(ZERO, value) if value is not None else None


def compute_allowance_plan(db: Session, user_id: str, year: int, today: date | None = None) -> dict[str, Any]:
    """Per-bank Freistellungsauftrag coverage, the best split and what to do (estimate, not tax advice)."""
    today = today or date.today()
    base: dict[str, Any] = {
        "tax_year": year, "estimate": True, "not_tax_advice": True, "label": ESTIMATE_DISCLAIMER,
        "applicable": False, "reason": None, "banks": [], "actions": [], "sources": SOURCES,
    }
    allowed, reason = _estimation_allowed(db)
    if not allowed:
        return {**base, "reason": reason}
    jurisdiction = get_user_jurisdiction(db, user_id, year)
    if jurisdiction != "DE":
        return {**base, "reason": f"Tax residence for {year} is {jurisdiction}: a Freistellungsauftrag and an "
                "NV-Bescheinigung need unlimited German tax liability (Sec. 44a(1) EStG)."}

    settings = settings_for_user(db, user_id)
    spouse = bool(settings.get("tax_spouse_allowance"))
    allowance = _dec(settings.get("freistellungsauftrag_amount")) or (
        SPARER_PAUSCHBETRAG_SPOUSE if spouse else SPARER_PAUSCHBETRAG_SINGLE)
    # What DKB and Scalable can get is the allowance minus the orders on file
    # elsewhere; an order can't be below what that bank already used.
    used_elsewhere = max(ZERO, _dec(settings.get("freistellungsauftrag_used")) or ZERO)
    other_banks = max(used_elsewhere, _dec(settings.get("freistellungsauftrag_other_banks_eur")) or ZERO)
    nv = nv_status(settings, year)
    church_rate = _church_rate(db)
    past, ahead = _year_fractions(year, today)

    pots: dict[str, _Pots] = {bank: _Pots() for bank, _ in BANKS}
    unassigned = _Pots()  # no bank recorded
    elsewhere = _Pots()  # booked at a bank other than DKB and Scalable
    unassigned_count = 0
    vorab_booked_isins: dict[str | None, set[str]] = {}
    for ev in list_events(db, user_id, year):
        bank = _bank_of(ev)
        if ev.event_type == "vorabpauschale" and ev.isin:
            vorab_booked_isins.setdefault(bank, set()).add(ev.isin.upper())
        if bank in pots:
            pots[bank].add(ev)
        elif ev.event_type not in ("dividend", "interest", "sale", "vorabpauschale"):
            continue
        elif bank is None:
            unassigned.add(ev)
            unassigned_count += 1
        else:
            elsewhere.add(ev)

    # January's Vorabpauschale is booked against this year's allowance (Sec. 18(3) InvStG).
    vorab_est, vorab_unassigned = (
        _vorab_by_bank(db, user_id, year, vorab_booked_isins) if year <= today.year + 1 else ({}, ZERO)
    )
    vorab_booked = today >= first_working_day(year)

    # The same months of last year, for dividends still to come.
    last_year_divs: dict[str, Decimal] = {}
    if ahead > 0:
        # 29 February has no twin in a common year; 28 February stands in.
        cutoff = (date(year - 1, today.month, min(today.day, 28 if today.month == 2 else today.day))
                  if year == today.year else date(year - 1, 1, 1))
        for ev in list_events(db, user_id, year - 1):
            bank = _bank_of(ev)
            if bank in pots and ev.event_type == "dividend" and ev.event_date >= cutoff:
                tf = Decimal(str(ev.teilfreistellung_pct or 0))
                last_year_divs[bank] = last_year_divs.get(bank, ZERO) + apply_teilfreistellung(
                    Decimal(str(ev.gross_eur or 0)), tf)

    savings = _tagesgeld(db, user_id, settings)
    rate_withheld = withholding_rate(church_rate)
    harvestable = _harvestable_by_bank(db, user_id)

    rows: list[BankYear] = []
    detail: dict[str, dict[str, Any]] = {}
    loss_left: dict[str, tuple[Decimal, Decimal]] = {}
    for bank, label in BANKS:
        p = pots[bank]
        vorab_estimate = vorab_est.get(bank, ZERO)
        if vorab_booked:
            p.vorab += vorab_estimate
        booked, aktien_left, other_left = p.taxable()
        loss_left[bank] = (aktien_left, other_left)
        # Credits booked once the order was used up arrived after tax, so the
        # synced amount is net: gross that part up (estimate).
        fsa = _fsa_setting(settings, bank)
        grossed_up = ZERO
        if fsa is not None and not (nv["valid_for_year"] and nv["filed"][bank]) and booked > fsa:
            opening_vorab = vorab_estimate if vorab_booked else ZERO
            net_above = min(booked - fsa, _net_credits_above(p.events, fsa, opening_vorab))
            grossed_up = net_above * rate_withheld / (Decimal("1") - rate_withheld)
            booked += grossed_up
        balance, rate = savings.get(bank, (ZERO, None))
        if rate is not None:
            interest_ahead = balance * rate * ahead
            interest_basis = "rate"
        elif past > Decimal("0.05") and p.interest > 0:
            interest_ahead = p.interest / past * ahead
            interest_basis = "run_rate"
        else:
            interest_ahead = ZERO
            interest_basis = "unknown" if balance > 0 else "none"
        dividends_ahead = last_year_divs.get(bank, ZERO)
        expected = interest_ahead + dividends_ahead + (ZERO if vorab_booked else vorab_estimate)
        rows.append(BankYear(
            bank=bank, label=label, fsa_eur=fsa,
            nv_filed=bool(nv["filed"][bank]), booked_eur=booked, expected_rest_eur=expected,
            harvestable_eur=harvestable.get(bank, ZERO),
        ))
        detail[bank] = {
            "booked": {"dividends_eur": p.dividends, "interest_eur": p.interest, "vorabpauschale_eur": p.vorab,
                       "gains_eur": p.gain_aktien + p.gain_other,
                       "losses_eur": p.loss_aktien + p.loss_other,
                       "withheld_grossed_up_eur": grossed_up},
            "vorabpauschale_estimated_eur": vorab_estimate,
            "expected": {"interest_eur": interest_ahead, "interest_basis": interest_basis,
                         "dividends_eur": dividends_ahead,
                         "vorabpauschale_eur": ZERO if vorab_booked else vorab_estimate},
            "savings_balance_eur": balance, "interest_rate": rate,
            "loss_left": {"aktien_eur": aktien_left, "other_eur": other_left},
            "harvestable_gain_eur": harvestable.get(bank, ZERO),
        }

    plan = plan_split(rows, allowance_eur=allowance, other_banks_eur=other_banks,
                      nv_valid=bool(nv["valid_for_year"]), church_rate=church_rate)
    unassigned_taxable, _, _ = unassigned.taxable()
    elsewhere_taxable, _, _ = elsewhere.taxable()
    projected_total = (sum((b.projected_eur for b in plan.banks), ZERO) + unassigned_taxable + elsewhere_taxable
                       + vorab_unassigned)
    eligibility = nv_eligibility(
        year, other_income_eur=_dec(settings.get("tax_other_income_eur")) or ZERO,
        capital_income_eur=projected_total, spouse=spouse,
    )
    actions = _actions(plan, nv, eligibility, rows, loss_left, year, today, unassigned_count, settings,
                       projected_total, allowance, church_rate)
    if settings.get("_bank_settings_other_user"):
        actions.append({"code": "bank_settings_other_user", "severity": "info",
                        "title": "The per-bank settings belong to another account",
                        "message": "Another user on this installation entered the Freistellungsaufträge and NV copies, "
                                   "so they aren't applied to your plan.",
                        "bank": None, "deadline": None, "how_to": None})

    def money(value: Decimal | None) -> float | None:
        return None if value is None else float(Decimal(value).quantize(Decimal("0.01")))

    def deep(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return money(obj)
        if isinstance(obj, dict):
            return {k: deep(v) for k, v in obj.items()}
        return obj

    return {
        **base,
        "applicable": True,
        "allowance_eur": money(allowance),
        "other_banks_eur": money(other_banks),
        "available_eur": money(plan.available_eur),
        "assigned_eur": money(plan.assigned_eur),
        "over_assigned": plan.over_assigned,
        "withholding_rate": float(withholding_rate(church_rate)),
        "projected_withheld_eur": money(plan.projected_withheld_eur),
        "recommended_withheld_eur": money(plan.recommended_withheld_eur),
        "changes_needed": plan.changes_needed,
        "projected_capital_income_eur": money(projected_total),
        "unassigned": {"events": unassigned_count, "taxable_eur": money(unassigned_taxable),
                       "vorabpauschale_eur": money(vorab_unassigned)},
        "other_banks_income_eur": money(elsewhere_taxable),
        "nv": {**nv, "eligibility": {
            "limit_eur": money(eligibility.limit_eur), "income_eur": money(eligibility.income_eur),
            "headroom_eur": money(eligibility.headroom_eur), "likely_eligible": eligibility.likely_eligible,
            "grundfreibetrag_assumed": eligibility.grundfreibetrag_assumed,
        }},
        "banks": [
            {
                "bank": b.bank, "label": b.label, "fsa_eur": money(b.fsa_eur), "nv_filed": b.nv_filed,
                "nv_covers": b.nv_covers, "booked_eur": money(b.booked_eur), "used_eur": money(b.used_eur),
                "remaining_eur": money(b.remaining_eur), "projected_eur": money(b.projected_eur),
                "projected_uncovered_eur": money(b.projected_uncovered_eur),
                "projected_withheld_eur": money(b.projected_withheld_eur),
                "minimum_fsa_eur": money(b.minimum_fsa_eur),
                "recommended_fsa_eur": money(b.recommended_fsa_eur),
                "recommended_withheld_eur": money(b.recommended_withheld_eur),
                "change_eur": money(b.change_eur),
                "how_to": HOW_TO.get(b.bank, {}),
                **deep(detail[b.bank]),
            }
            for b in plan.banks
        ],
        "actions": actions,
    }


def _deadline(year: int, month_day: tuple[int, int]) -> str:
    return date(year, *month_day).isoformat()


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(Decimal('1')):,}".replace(",", ".") + " €"


def _actions(
    plan: SplitPlan, nv: dict[str, Any], eligibility: Any, rows: list[BankYear],
    loss_left: dict[str, tuple[Decimal, Decimal]], year: int, today: date, unassigned_count: int,
    settings: dict[str, Any], projected_total: Decimal, allowance: Decimal, church_rate: Decimal,
) -> list[dict[str, Any]]:
    """What to do, most urgent first. Each item names the bank and the deadline where one applies."""
    actions: list[dict[str, Any]] = []
    current = year == today.year
    by_bank = {b.bank: b for b in plan.banks}

    def add(code: str, severity: str, title: str, message: str, *, bank: str | None = None,
            deadline: str | None = None, how_to: str | None = None) -> None:
        actions.append({"code": code, "severity": severity, "title": title, "message": message,
                        "bank": bank, "deadline": deadline, "how_to": how_to})

    # 1. NV certificate missing at a bank that will pay income.
    if nv["valid_for_year"] and nv["banks_known"]:
        for b in plan.banks:
            if not b.nv_filed and b.projected_eur > 0:
                add("nv_missing_at_bank", "action", f"Send the NV certificate to {b.label}",
                    f"{b.label} doesn't have your NV certificate, so it withholds tax on everything above its "
                    f"Freistellungsauftrag ({_fmt(b.fsa_eur or Decimal('0'))}): about "
                    f"{_fmt(b.projected_withheld_eur)} this year on {_fmt(b.projected_eur)} of projected income. "
                    "A copy is enough.",
                    bank=b.bank, deadline=_deadline(year, NV_SCALABLE_DEADLINE) if b.bank == "scalable" and current
                    else None, how_to=HOW_TO.get(b.bank, {}).get("nv"))
    elif nv["has_certificate"] and not nv["banks_known"]:
        add("nv_banks_unknown", "warning", "Which banks have your NV certificate?",
            "Tick it per bank in the tax settings. Until then the cockpit assumes every bank has a copy, and a bank "
            "that doesn't will withhold tax.")
    if nv["ends_midyear"]:
        add("nv_ends_midyear", "warning", f"The NV certificate ends on {nv['valid_until']}, before the year is over",
            "A certificate normally runs to 31 December; one that ends earlier was revoked (or the date is mistyped). "
            "From then on each bank applies its Freistellungsauftrag again, so this plan treats the year as not "
            "covered.")
    elif nv["has_certificate"] and not nv["valid_for_year"]:
        add("nv_expired", "warning", "The NV certificate has expired",
            f"It ran until {nv['valid_until']}. Banks withhold tax again until a new one is filed; apply at the "
            "Finanzamt (form NV 1 A, also in ELSTER).")
    elif nv["ends_this_year"]:
        add("nv_ends", "info", f"The NV certificate ends on 31 December {year}",
            "Apply for the next one in time (form NV 1 A at the Finanzamt or in ELSTER) and send a copy to every bank.")
    if nv["valid_for_year"] and not eligibility.likely_eligible:
        add("nv_return", "warning", "Your income may be too high for the NV certificate",
            f"Projected income ({_fmt(eligibility.income_eur)}) is above the limit of {_fmt(eligibility.limit_eur)}. "
            "If the conditions no longer hold you must hand it back to the Finanzamt (Sec. 44a(2) sentence 4 EStG).")

    # 2. The split.
    if plan.over_assigned:
        add("fsa_over_assigned", "action", "Your Freistellungsaufträge add up to more than the allowance",
            f"{_fmt(plan.assigned_eur + plan.other_banks_eur)} is on file but the allowance is {_fmt(allowance)}. "
            "The banks report every order to the Bundeszentralamt für Steuern; lower one so the total fits.")
    for row in rows:
        b = by_bank[row.bank]
        if row.fsa_eur is None and not b.nv_covers and b.projected_eur > 0:
            add("fsa_unknown", "info", f"Enter the Freistellungsauftrag you gave {b.label}",
                "Without it the cockpit can't tell how much tax the bank withholds. Enter 0 if you gave none.",
                bank=b.bank)
    moves = [b for b in plan.banks if b.change_eur != 0]
    saving = plan.projected_withheld_eur - plan.recommended_withheld_eur
    if moves and (saving > Decimal("1") or plan.over_assigned):
        parts = ", ".join(f"{b.label} {_fmt(b.fsa_eur or Decimal('0'))} → {_fmt(b.recommended_fsa_eur)}"
                          for b in moves)
        lowered_under_nv = [b.label for b in moves if b.nv_covers and b.change_eur < 0]
        later = (f" The NV certificate makes {', '.join(lowered_under_nv)}'s order idle this year; give it one "
                 "again for the year the certificate ends." if lowered_under_nv else "")
        add("fsa_resplit", "action" if saving > Decimal("1") else "info", "Change the Freistellungsauftrag split",
            f"{parts}. That lowers the tax the banks withhold this year by about {_fmt(saving)}. A bank can't go "
            f"below what it has already used.{later}",
            deadline=_deadline(year, FSA_CHANGE_DEADLINE) if current else None,
            how_to=" ".join(HOW_TO[b.bank]["fsa"] for b in moves if b.bank in HOW_TO))
    # Withholding the allowance (or, with a valid NV certificate, the Grundfreibetrag) would have
    # covered is not lost, but only the tax return brings it back.
    refundable = plan.projected_withheld_eur if (
        (nv["valid_for_year"] and eligibility.likely_eligible) or projected_total <= allowance
    ) else Decimal("0")
    if refundable > Decimal("1"):
        add("anlage_kap", "info", "Withheld tax comes back only with a tax return",
            f"With the current orders about {_fmt(refundable)} is withheld although your allowance would cover it. "
            "Fix the split or the NV certificate first; whatever is withheld anyway comes back through Anlage KAP "
            "(Sec. 32d(4) EStG) with each bank's Steuerbescheinigung.")

    # 3. Losses at one bank, gains at the other.
    for bank, (aktien_left, other_left) in loss_left.items():
        if aktien_left + other_left <= 0:
            continue
        others = [b for b in plan.banks if b.bank != bank and b.projected_eur > 0]
        if others:
            add("verlustbescheinigung", "action", f"Ask {BANK_LABELS[bank]} for a Verlustbescheinigung",
                f"{BANK_LABELS[bank]} holds {_fmt(aktien_left + other_left)} of losses it can't offset, while "
                f"{', '.join(o.label for o in others)} books income. Banks don't offset across each other; with the "
                "certificate the tax return can.",
                bank=bank, deadline=_deadline(year, VERLUSTBESCHEINIGUNG_DEADLINE) if current else None,
                how_to=HOW_TO.get(bank, {}).get("loss"))

    # 4. NV certificate worth applying for.
    if not nv["valid_for_year"] and eligibility.likely_eligible:
        add("nv_apply", "info", "You may qualify for an NV certificate",
            f"Projected income {_fmt(eligibility.income_eur)} is within {_fmt(eligibility.limit_eur)} (Grundfreibetrag "
            "plus Sparer-Pauschbetrag). With an NV certificate filed at both banks nothing is withheld and gains up "
            "to that limit can be realised tax-free. Apply at the Finanzamt with form NV 1 A (also in ELSTER). It is "
            "not issued when a loss carry-forward is assessed or you file a tax return on application "
            "(BMF 14.05.2025 Rn. 252).")

    if unassigned_count:
        add("events_without_bank", "info", f"{unassigned_count} tax events have no bank",
            "They count toward the yearly total but not toward any bank's Freistellungsauftrag. Set the bank on "
            "each manual event.")
    order = {"action": 0, "warning": 1, "info": 2}
    actions.sort(key=lambda a: order.get(a["severity"], 3))
    return actions


# --- gain harvesting (needs the per-bank plan above) ----------------------------


def compute_gain_harvest(db: Session, user_id: str, year: int, today: date | None = None) -> dict[str, Any]:
    """Gain harvesting for ``year``: realise gains tax-free, buy the units straight back.

    With a valid NV certificate (``mode = "nv"``) the room is the Grundfreibetrag
    plus the Sparer-Pauschbetrag less other income and the capital income already
    booked; a bank without a copy of the certificate still withholds above its
    Freistellungsauftrag, so the gains realised there are capped at what that
    order still covers. Without one (``mode = "allowance"``) the room is what each
    bank's Freistellungsauftrag still covers by the end of the year. Oldest lots
    first, as the law requires. Estimates only; broker statements remain the
    source of truth.
    """
    today = today or date.today()
    base: dict[str, Any] = {
        "tax_year": year, "estimate": True, "not_tax_advice": True, "label": ESTIMATE_DISCLAIMER,
        "enabled": False, "reason": None, "steps": [], "skipped": [], "warnings": [], "bank_rooms": [],
    }
    settings = settings_for_user(db, user_id)
    overview = compute_year_overview(db, user_id, year)
    if overview.get("disabled"):
        return {**base, "reason": overview.get("reason")}
    allowance_plan = compute_allowance_plan(db, user_id, year, today)
    if not allowance_plan.get("applicable"):
        return {**base, "reason": allowance_plan.get("reason")}
    banks = allowance_plan["banks"]
    nv = nv_status(settings, year)
    mode = "nv" if nv["valid_for_year"] else "allowance"
    capital = Decimal(str(overview["lines"]["taxable_income_before_allowance_eur"]))

    def fsa_left(bank: dict[str, Any]) -> Decimal:
        return max(Decimal("0"), Decimal(str(bank["fsa_eur"] or 0)) - Decimal(str(bank["projected_eur"] or 0)))

    room_payload: dict[str, Any]
    bank_room: dict[str, Decimal] = {}
    if mode == "nv":
        warnings, nv_info = _nv_warnings(settings, today)
        # The whole year's income uses the room, not only what is booked: the
        # interest, dividends and January Vorabpauschale still to come too.
        projected = Decimal(str(allowance_plan.get("projected_capital_income_eur") or 0))
        insurance = settings.get("tax_health_insurance") or "unknown"
        room = tax_free_room(
            year,
            other_income_eur=Decimal(str(settings.get("tax_other_income_eur") or 0)),
            capital_income_so_far_eur=max(capital, projected),
            spouse=bool(settings.get("tax_spouse_allowance")),
            german_family_insurance=insurance in FAMILY_LIMIT_INSURANCE,
        )

        if room.grundfreibetrag_assumed:
            warnings.append({"code": "grundfreibetrag_assumed", "message": f"The Grundfreibetrag for {year} is "
                             "not in the table yet; the latest known one is used."})
        room_eur = room.room_eur
        for bank in banks:
            if not bank["nv_covers"]:
                bank_room[bank["bank"]] = fsa_left(bank)
                warnings.append({"code": "nv_not_at_bank", "message": f"{bank['label']} has no copy of the NV "
                                 f"certificate: gains realised there are kept within its Freistellungsauftrag "
                                 f"({fsa_left(bank):.0f} € left this year). Send it a copy to harvest more."})
        room_payload = {
            "mode": mode,
            "grundfreibetrag_eur": float(room.grundfreibetrag_eur),
            "sparer_pauschbetrag_eur": float(room.sparer_pauschbetrag_eur),
            "other_income_eur": float(room.other_income_eur),
            "capital_income_so_far_eur": float(capital),
            "projected_capital_income_eur": float(room.capital_income_so_far_eur),
            "tax_free_room_eur": float(room.tax_free_room_eur),
            "family_insurance_room_eur": (
                float(room.family_insurance_room_eur) if room.family_insurance_room_eur is not None else None
            ),
            "room_eur": float(room.room_eur),
        }
    else:
        nv_info = None
        if all(bank["fsa_eur"] is None for bank in banks):
            return {**base, "mode": mode, "reason": "Enter the Freistellungsauftrag you gave each bank in the tax "
                    "settings (or file an NV certificate) to plan tax-free gain harvesting."}
        warnings = []
        # The allowance each bank's income takes up: only up to its own order (above
        # it the bank withholds). Income at other banks sits under their orders,
        # which ``available_eur`` already leaves out; events without a bank count, to be safe.
        unassigned = allowance_plan["unassigned"]
        covered = sum((min(Decimal(str(bank["fsa_eur"] or 0)), Decimal(str(bank["projected_eur"] or 0)))
                       for bank in banks), Decimal("0"))
        covered += Decimal(str(unassigned["taxable_eur"] or 0)) + Decimal(str(unassigned["vorabpauschale_eur"] or 0))
        room_eur = max(Decimal("0"), Decimal(str(allowance_plan["available_eur"] or 0)) - covered)
        bank_room = {bank["bank"]: fsa_left(bank) for bank in banks}
        room_eur = min(room_eur, sum(bank_room.values(), Decimal("0")))
        warnings.append({"code": "allowance_only", "message": "Without an NV certificate only what each bank's "
                         "Freistellungsauftrag still covers this year is tax-free; the room already allows for the "
                         "interest and dividends still expected."})
        room_payload = {
            "mode": mode,
            "sparer_pauschbetrag_eur": float(allowance_plan["allowance_eur"] or 0),
            "capital_income_so_far_eur": float(capital),
            "projected_capital_income_eur": float(allowance_plan["projected_capital_income_eur"] or 0),
            "room_eur": float(room_eur),
        }
    if year < today.year:
        warnings.append({"code": "past_year", "message": f"{year} is over; nothing can be realised in it any more."})
    elif year > today.year:
        # A sale placed now settles in this year and uses this year's room.
        warnings.append({"code": "future_year", "message": f"Gains for {year} can only be realised in {year}; "
                         f"a sale now counts for {today.year}. Plan these trades from January {year}."})
    else:
        warnings.append({"code": "settle_in_year", "message": "Trade by mid-December so the sale settles in the "
                         "tax year, and buy the same units back the same day."})

    holdings, skipped, lot_warnings = _harvest_holdings(db, user_id)
    steps = [] if year != today.year else plan_gain_harvest(
        holdings, room_eur, _harvest_order_fee, HARVEST_SPREAD_BPS, bank_room=bank_room or None,
    )
    return {
        **base,
        "enabled": True,
        "mode": mode,
        "nv_certificate": nv_info,
        "health_insurance": settings.get("tax_health_insurance") or "unknown",
        "room": room_payload,
        "bank_rooms": [
            {"bank": bank["bank"], "label": bank["label"], "nv_covers": bank["nv_covers"],
             "room_eur": float(bank_room[bank["bank"]]) if bank["bank"] in bank_room else None}
            for bank in banks
        ],
        "steps": [
            {
                "isin": s.isin, "name": s.name, "sell_quantity": float(s.sell_quantity),
                "notional_eur": float(s.notional_eur), "gain_eur": float(s.gain_eur),
                "taxable_gain_eur": float(s.taxable_gain_eur), "trading_cost_eur": float(s.trading_cost_eur),
                "future_tax_avoided_eur": float(s.future_tax_avoided_eur),
                "net_benefit_eur": float(s.net_benefit_eur), "whole_position": s.whole_position,
                "bank": s.bank,
            }
            for s in steps
        ],
        "totals": {
            "taxable_gain_eur": float(sum((s.taxable_gain_eur for s in steps), Decimal("0"))),
            "trading_cost_eur": float(sum((s.trading_cost_eur for s in steps), Decimal("0"))),
            "future_tax_avoided_eur": float(sum((s.future_tax_avoided_eur for s in steps), Decimal("0"))),
        },
        "skipped": skipped,
        "warnings": warnings + lot_warnings,
    }


def tax_position(db: Session, user_id: str, year: int, today: date | None = None) -> dict[str, Any]:
    """What the banks will withhold this year against what is finally owed.

    The year overview applies the flat 25 % to everything above one
    allowance, which is what a bank without an NV copy would withhold, not
    what is owed: with an NV certificate nothing is withheld at that bank, and
    with little other income the Guenstigerpruefung (Sec. 32d(6) EStG) taxes
    capital income at the personal rate, which is zero up to the
    Grundfreibetrag. Estimate, not tax advice.
    """
    from app.foundation.tax_calc.jurisdictions.de.income_tax import guenstigerpruefung

    plan = compute_allowance_plan(db, user_id, year, today)
    base = {"estimate": True, "not_tax_advice": True, "tax_year": year}
    if not plan.get("applicable"):
        return {**base, "applicable": False, "reason": plan.get("reason")}
    settings = settings_for_user(db, user_id)
    spouse = bool(settings.get("tax_spouse_allowance"))
    church_rate = _church_rate(db)
    projected = Decimal(str(plan.get("projected_capital_income_eur") or 0))
    other_income = _dec(settings.get("tax_other_income_eur")) or ZERO
    g = guenstigerpruefung(year, capital_income_eur=projected, other_income_eur=other_income, spouse=spouse)
    flat_total = (g.capital_taxable_eur * withholding_rate(church_rate)).quantize(Decimal("0.01"))
    final = g.tariff_tax_eur if g.use_tariff else flat_total
    withheld = Decimal(str(plan.get("projected_withheld_eur") or 0))
    refund = max(ZERO, withheld - final)
    owed = max(ZERO, final - withheld)
    nv = plan.get("nv") or {}
    if final == 0 and withheld == 0:
        headline = "No tax on this year's capital income, and none withheld."
    elif final == 0:
        how = "Anlage KAP with the Günstigerprüfung" if g.use_tariff else "Anlage KAP"
        headline = (f"No tax is owed, but the banks will withhold about {_fmt(withheld)}; "
                    f"a tax return with {how} gets it back.")
    elif refund > 0:
        headline = f"About {_fmt(final)} is owed; a return would get back about {_fmt(refund)} of what is withheld."
    elif owed > 0:
        headline = f"About {_fmt(owed)} more than the banks withhold is owed; a return is required."
    else:
        headline = f"About {_fmt(final)} is owed, and the banks withhold it."
    return {
        **base,
        "applicable": True,
        "projected_capital_income_eur": float(projected.quantize(Decimal("0.01"))),
        "other_income_eur": float(other_income),
        "withheld_by_banks_eur": float(withheld.quantize(Decimal("0.01"))),
        "flat_rate_tax_eur": float(flat_total),
        "tariff_tax_eur": float(g.tariff_tax_eur),
        "tariff_assumed": g.tariff_assumed,
        "use_guenstigerpruefung": g.use_tariff,
        "final_tax_eur": float(final),
        "refund_with_return_eur": float(refund.quantize(Decimal("0.01"))),
        "owed_with_return_eur": float(owed.quantize(Decimal("0.01"))),
        "nv_valid_for_year": bool(nv.get("valid_for_year")),
        "headline": headline,
        "notes": [
            "Final tax: the lower of the flat 25 % (plus Soli and church tax) and the personal tariff on capital "
            "income above the Sparer-Pauschbetrag and the 36 € Sonderausgaben-Pauschbetrag.",
            "Under the tariff, church tax and Soli are left out; below the Soli exemption limit there is none.",
        ],
    }
