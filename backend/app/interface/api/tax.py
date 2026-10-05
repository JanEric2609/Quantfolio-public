"""Tax cockpit API — estimates only.

All responses include ``estimate: true`` and ``not_tax_advice: true``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.foundation.core.db import get_db
from app.foundation.models.entities import TaxLedgerEvent, TaxLot, User
from app.foundation.schemas import (
    BasiszinsListOut,
    DeletedOut,
    GainHarvestOut,
    JurisdictionsOut,
    KapReportOut,
    ResidencyListOut,
    TaxEventsListOut,
    TaxLotsListOut,
    TaxMutationOut,
    TaxOverviewOut,
    TaxAllowancePlanOut,
    TaxSettingsEnvelope,
    VorabpauschaleEstimateOut,
)
from app.foundation.auth import current_user
from app.foundation.settings import upsert_public_settings
from app.foundation.tax_allowances import (
    BankSettingsOwnedByOtherUser,
    claim_bank_settings,
    compute_allowance_plan,
    compute_gain_harvest,
    tax_position,
)
from app.foundation.tax_cockpit import (
    TAX_SETTING_KEYS,
    _disabled_payload,
    _estimation_allowed,
    compute_year_overview,
    create_event,
    create_lot,
    estimate_vorabpauschale,
    event_institution,
    get_tax_settings,
    list_events,
    list_lots_with_provenance,
    record_sale_via_fifo,
    seed_lots_from_activity,
)
from app.foundation.tax_calc.vorabpauschale import BASISZINS_BY_YEAR


router = APIRouter(prefix="/api/tax", tags=["tax"])


def _tax_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Enforce the mandated tax-response contract (audit §6.5).

    Every response carries ``estimate: true`` and ``not_tax_advice: true``;
    disabled/ineligible responses additionally carry empty ``lines``, ``tax``
    and ``provenance`` structures so frontend destructuring (OverviewTab)
    cannot crash. Strictly additive: existing keys are never overwritten.
    """
    payload.setdefault("estimate", True)
    payload.setdefault("not_tax_advice", True)
    if payload.get("disabled"):
        payload.setdefault("lines", [])
        payload.setdefault("tax", {})
        payload.setdefault("provenance", {})
    return payload


class TaxEventIn(BaseModel):
    event_type: str = Field(
        ..., description="dividend|interest|sale|vorabpauschale|withholding|fee"
    )
    event_date: date
    isin: str | None = None
    symbol: str | None = None
    name: str | None = None
    fund_class: str | None = None
    teilfreistellung_pct: float | None = None
    gross_eur: float = 0
    withheld_eur: float = 0
    foreign_wht_eur: float = 0
    foreign_country: str | None = None
    realised_gain_eur: float | None = None
    bucket: str | None = None
    source: str = "manual"
    source_ref: str | None = None
    account_ref: str | None = None
    # The bank that booked it ("dkb", "scalable", …); each bank applies only its
    # own Freistellungsauftrag.
    institution: str | None = Field(default=None, max_length=32)
    notes: str | None = None


class TaxLotIn(BaseModel):
    isin: str
    symbol: str | None = None
    name: str | None = None
    account_ref: str | None = None
    fund_class: str = "other"
    teilfreistellung_pct: float | None = None
    acquired_at: date
    quantity: float
    cost_basis_eur: float
    fees_eur: float = 0
    fx_rate: float | None = None
    source: str = "manual"
    source_ref: str | None = None
    account_ref: str | None = None
    notes: str | None = None


class TaxSaleIn(BaseModel):
    isin: str
    sell_date: date
    sell_quantity: float
    sell_price_per_unit_eur: float
    sell_fees_eur: float = 0
    fund_class: str | None = None
    bucket: str | None = None
    source: str = "manual"
    source_ref: str | None = None
    account_ref: str | None = None
    institution: str | None = Field(default=None, max_length=32)


class VorabpauschaleIn(BaseModel):
    isin: str
    fund_value_start_of_year_eur: float
    fund_value_end_of_year_eur: float
    distributions_during_year_eur: float = 0
    tax_year: int
    fund_class: str | None = None
    months_held: int = 12


# Sending null for these resets them to "not entered".
_CLEARABLE_TAX_SETTINGS = frozenset({
    "freistellungsauftrag_dkb_eur",
    "freistellungsauftrag_scalable_eur",
    "freistellungsauftrag_other_banks_eur",
    "tax_interest_rate_dkb",
    "tax_nv_filed_dkb",
    "tax_nv_filed_scalable",
})


class TaxSettingsIn(BaseModel):
    tax_residency_country: str | None = None
    church_tax: str | None = None
    freistellungsauftrag_amount: float | None = None
    freistellungsauftrag_used: float | None = None
    freistellungsauftrag_dkb_eur: float | None = Field(default=None, ge=0, le=2000)
    freistellungsauftrag_scalable_eur: float | None = Field(default=None, ge=0, le=2000)
    freistellungsauftrag_other_banks_eur: float | None = Field(default=None, ge=0, le=2000)
    tax_interest_rate_dkb: float | None = Field(default=None, ge=0, le=0.2)
    tax_spouse_allowance: bool | None = None
    tax_estimation_enabled: bool | None = None
    tax_nv_certificate: bool | None = None
    # An empty string clears it.
    tax_nv_valid_until: date | Literal[""] | None = None
    tax_nv_filed_dkb: bool | None = None
    tax_nv_filed_scalable: bool | None = None
    tax_other_income_eur: float | None = Field(default=None, ge=0)
    tax_health_insurance: Literal["unknown", "de_family", "de_own", "foreign"] | None = None
    tax_bafoeg: bool | None = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings", response_model_exclude_unset=True, response_model=TaxSettingsEnvelope)
def read_settings(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    return {"settings": get_tax_settings(db)}


@router.get("/basiszins", response_model_exclude_unset=True, response_model=BasiszinsListOut)
def read_basiszins(user: User = Depends(current_user)) -> dict[str, Any]:
    return {
        "items": [
            {"year": year, "basiszins": float(rate)}
            for year, rate in sorted(BASISZINS_BY_YEAR.items(), reverse=True)
        ]
    }


def _validate_tax_settings(current: dict[str, Any], updates: dict[str, Any]) -> None:
    """Reject what no bank would accept (HTTP 422).

    All Freistellungsauftraege together may not exceed the Sparer-Pauschbetrag
    (Sec. 44a(2) EStG: 1,000 EUR, 2,000 EUR for spouses), and an NV
    certificate is valid for at most three years, ending on 31 December
    (Sec. 44a(2) sentence 3 EStG).
    """
    merged = {**current, **updates}
    allowance = 2000.0 if merged.get("tax_spouse_allowance") else 1000.0
    orders = sum(
        float(merged.get(key) or 0)
        for key in ("freistellungsauftrag_dkb_eur", "freistellungsauftrag_scalable_eur",
                    "freistellungsauftrag_other_banks_eur")
    )
    if orders > allowance + 0.005:
        raise HTTPException(
            status_code=422,
            detail=f"The Freistellungsaufträge add up to {orders:,.0f} €, more than the {allowance:,.0f} € "
                   "allowance. Lower one of them first.",
        )
    until = updates.get("tax_nv_valid_until")
    if until:
        parsed = date.fromisoformat(str(until)[:10])
        if (parsed.month, parsed.day) != (12, 31):
            raise HTTPException(
                status_code=422,
                detail="An NV certificate always ends on 31 December; enter the date printed on it.",
            )


@router.put("/settings", response_model_exclude_unset=True, response_model=TaxSettingsEnvelope)
def write_settings(
    payload: TaxSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    sent = payload.model_dump(mode="json", exclude_unset=True)
    updates = {
        key: value
        for key, value in sent.items()
        # null clears these (back to "not entered"); other keys ignore it as before.
        if key in TAX_SETTING_KEYS and (value is not None or key in _CLEARABLE_TAX_SETTINGS)
    }
    _validate_tax_settings(get_tax_settings(db), updates)
    try:
        updates = claim_bank_settings(db, user.id, updates)
    except BankSettingsOwnedByOtherUser as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if updates:
        upsert_public_settings(db, updates)
    return {"settings": get_tax_settings(db)}


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

# REMOVED: replaced by jurisdiction-aware endpoint below (line ~424)
# The old DE-only /overview handler was here; it is superseded by
# get_tax_overview() which routes to DE or NL depending on jurisdiction.


@router.get("/reports/kap", response_model_exclude_unset=True, response_model=KapReportOut)
def report_kap(
    year: int = date.today().year,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    payload = compute_year_overview(db, user.id, year)
    if payload.get("disabled"):
        return _tax_response(
            {
                "tax_year": year,
                "form": "Anlage KAP",
                **payload,
            }
        )
    return _tax_response(
        {
            "tax_year": year,
            "form": "Anlage KAP",
            "estimate": True,
            "not_tax_advice": True,
            "lines": payload["anlage_kap_mapping"],
            "tax": payload["tax"],
            "allowance": payload["allowance"],
            "carryforward": payload["carryforward"],
        }
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@router.get("/events", response_model_exclude_unset=True, response_model=TaxEventsListOut)
def read_events(
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    events = list_events(db, user.id, year)
    return {
        "items": [
            {
                "id": ev.id,
                "tax_year": ev.tax_year,
                "event_date": ev.event_date.isoformat(),
                "event_type": ev.event_type,
                "isin": ev.isin,
                "symbol": ev.symbol,
                "name": ev.name,
                "fund_class": ev.fund_class,
                "teilfreistellung_pct": float(ev.teilfreistellung_pct or 0),
                "gross_eur": float(ev.gross_eur or 0),
                "withheld_eur": float(ev.withheld_eur or 0),
                "foreign_wht_eur": float(ev.foreign_wht_eur or 0),
                "foreign_country": ev.foreign_country,
                "realised_gain_eur": float(ev.realised_gain_eur)
                if ev.realised_gain_eur is not None
                else None,
                "bucket": ev.bucket,
                "confidence": ev.confidence,
                "source": ev.source,
                "source_ref": ev.source_ref,
                "institution": event_institution(ev),
                "notes": ev.notes,
            }
            for ev in events
        ],
        "estimate": True,
        "not_tax_advice": True,
    }


@router.post("/events", response_model_exclude_unset=True, response_model=TaxMutationOut)
def write_event(
    payload: TaxEventIn = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    event = create_event(db, user.id, payload.model_dump())
    return _tax_response({"id": event.id})


@router.delete("/events/{event_id}", response_model_exclude_unset=True, response_model=DeletedOut)
def delete_event(
    event_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    row = (
        db.query(TaxLedgerEvent)
        .filter(TaxLedgerEvent.id == event_id, TaxLedgerEvent.user_id == user.id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    db.delete(row)
    db.commit()
    return {"deleted": event_id}


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------


@router.get("/lots", response_model_exclude_unset=True, response_model=TaxLotsListOut)
def read_lots(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    return {
        "items": list_lots_with_provenance(db, user.id),
        "estimate": True,
        "not_tax_advice": True,
    }


@router.post("/lots", response_model_exclude_unset=True, response_model=TaxMutationOut)
def write_lot(
    payload: TaxLotIn = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    lot = create_lot(db, user.id, payload.model_dump())
    return _tax_response({"id": lot.id})


@router.delete("/lots/{lot_id}", response_model_exclude_unset=True, response_model=DeletedOut)
def delete_lot(
    lot_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    row = db.query(TaxLot).filter(TaxLot.id == lot_id, TaxLot.user_id == user.id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="lot not found")
    db.delete(row)
    db.commit()
    return {"deleted": lot_id}


@router.post(
    "/lots/seed-from-activity", response_model_exclude_unset=True, response_model=TaxMutationOut
)
def seed_from_activity(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    allowed, reason = _estimation_allowed(db)
    if not allowed:
        return _tax_response(_disabled_payload(reason))
    created = seed_lots_from_activity(db, user.id)
    return _tax_response({"created": created, "estimate": True, "not_tax_advice": True})


@router.post("/sales/fifo", response_model_exclude_unset=True, response_model=TaxMutationOut)
def record_sale(
    payload: TaxSaleIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    allowed, reason = _estimation_allowed(db)
    if not allowed:
        return _tax_response(_disabled_payload(reason))
    try:
        event = record_sale_via_fifo(
            db,
            user.id,
            isin=payload.isin,
            sell_date=payload.sell_date,
            sell_quantity=Decimal(str(payload.sell_quantity)),
            sell_price_per_unit_eur=Decimal(str(payload.sell_price_per_unit_eur)),
            sell_fees_eur=Decimal(str(payload.sell_fees_eur)),
            fund_class=payload.fund_class,
            bucket=payload.bucket,
            source=payload.source,
            source_ref=payload.source_ref,
            account_ref=payload.account_ref,
            institution=payload.institution,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _tax_response(
        {
            "id": event.id,
            "realised_gain_eur": float(event.realised_gain_eur or 0),
            "gross_eur": float(event.gross_eur or 0),
            "estimate": True,
            "not_tax_advice": True,
        }
    )


# ---------------------------------------------------------------------------
# Vorabpauschale
# ---------------------------------------------------------------------------


@router.post(
    "/vorabpauschale/estimate",
    response_model_exclude_unset=True,
    response_model=VorabpauschaleEstimateOut,
)
def vorabpauschale(
    payload: VorabpauschaleIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    return _tax_response(
        estimate_vorabpauschale(
            db=db,
            isin=payload.isin,
            fund_value_start_of_year_eur=Decimal(str(payload.fund_value_start_of_year_eur)),
            fund_value_end_of_year_eur=Decimal(str(payload.fund_value_end_of_year_eur)),
            distributions_during_year_eur=Decimal(str(payload.distributions_during_year_eur)),
            tax_year=payload.tax_year,
            fund_class=payload.fund_class,
            months_held=payload.months_held,
        )
    )


# ---------------------------------------------------------------------------
# Phase 7: Multi-jurisdiction support (DE, NL)
# ---------------------------------------------------------------------------


@router.get("/jurisdictions", response_model_exclude_unset=True, response_model=JurisdictionsOut)
def list_jurisdictions(user: User = Depends(current_user)) -> dict[str, Any]:
    """List supported tax jurisdictions."""
    return {
        "jurisdictions": [
            {"code": "DE", "name": "Germany", "label": "German income tax (KESt/Soli)"},
            {"code": "NL", "name": "Netherlands", "label": "Dutch asset tax (Box 3)"},
        ]
    }


class TaxResidencyIn(BaseModel):
    """Request body for setting a tax residency period."""

    country: Literal["DE", "NL"]
    valid_from: str | None = None
    valid_to: str | None = None

    @model_validator(mode="after")
    def _check_dates(self) -> "TaxResidencyIn":
        from datetime import date

        vf = date.fromisoformat(self.valid_from) if self.valid_from else date.today()
        vt = date.fromisoformat(self.valid_to) if self.valid_to else None
        if vt is not None and vt < vf:
            raise ValueError("valid_to must be on or after valid_from")
        # Normalize dates so downstream set_residency uses consistent values.
        self.valid_from = vf.isoformat()
        self.valid_to = vt.isoformat() if vt else None
        return self


@router.post("/residency", response_model_exclude_unset=True, response_model=TaxMutationOut)
def set_residency(
    payload: TaxResidencyIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Record a tax residency period."""
    from app.foundation.models.entities import TaxResidencyPeriod
    from datetime import date

    country = payload.country
    # _check_dates already normalized these to ISO strings (or None).
    assert payload.valid_from is not None  # guaranteed by _check_dates
    valid_from = date.fromisoformat(payload.valid_from)
    valid_to = date.fromisoformat(payload.valid_to) if payload.valid_to else None

    period = TaxResidencyPeriod(
        user_id=user.id, country=country, valid_from=valid_from, valid_to=valid_to
    )
    db.add(period)
    db.commit()
    return _tax_response(
        {"id": period.id, "country": country, "valid_from": valid_from.isoformat()}
    )


@router.get("/residency", response_model_exclude_unset=True, response_model=ResidencyListOut)
def get_residency(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get current tax residency periods."""
    from app.foundation.models.entities import TaxResidencyPeriod

    periods = (
        db.query(TaxResidencyPeriod)
        .filter(TaxResidencyPeriod.user_id == user.id)
        .order_by(TaxResidencyPeriod.valid_from.desc())
        .all()
    )
    return {
        "periods": [
            {
                "id": p.id,
                "country": p.country,
                "valid_from": p.valid_from.isoformat(),
                "valid_to": p.valid_to.isoformat() if p.valid_to else None,
            }
            for p in periods
        ]
    }


@router.get("/box3", response_model_exclude_unset=True, response_model=TaxOverviewOut)
def get_box3(
    year: int,
    bank_deposits: float = 0.0,
    investments: float = 0.0,
    debts: float = 0.0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get NL Box 3 (wealth tax) calculation.

    Pass bucket amounts directly via query params (bank_deposits, investments, debts)
    to get a meaningful calculation. Without them the calculator defaults to zero wealth.
    """
    from app.foundation.tax_cockpit import compute_box3_year_overview

    holdings: list[dict] = []
    if bank_deposits > 0:
        holdings.append({"type": "bank_account", "amount_eur": bank_deposits})
    if investments > 0:
        holdings.append({"type": "equity", "amount_eur": investments})
    if debts > 0:
        holdings.append({"type": "mortgage_debt", "amount_eur": debts})

    return _tax_response(compute_box3_year_overview(db, user.id, year, holdings=holdings))


@router.get("/overview", response_model_exclude_unset=True, response_model=TaxOverviewOut)
def get_tax_overview(
    year: int,
    jurisdiction: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Get tax overview for a given jurisdiction (or auto-detected)."""
    from app.foundation.tax_cockpit import compute_year_overview_by_jurisdiction

    result = compute_year_overview_by_jurisdiction(db, user.id, year, jurisdiction)
    if not result.get("disabled") and result.get("lines") is not None and "wealth" not in result:
        result["position"] = tax_position(db, user.id, year)
    return _tax_response(result)


@router.get("/harvest", response_model_exclude_unset=True, response_model=GainHarvestOut)
def get_gain_harvest(
    year: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Tax-free gain harvesting under an NV certificate (estimate, not tax advice)."""
    return compute_gain_harvest(db, user.id, year)


@router.get("/allowances", response_model_exclude_unset=True, response_model=TaxAllowancePlanOut)
def get_allowances(
    year: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Freistellungsauftrag and NV certificate per bank: coverage, best split, what to do (estimate)."""
    return _tax_response(compute_allowance_plan(db, user.id, year))
