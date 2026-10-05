"""German private-investor tax cockpit -- estimates only.

Every output here is an *estimate*. Broker statements (Erträgnisaufstellung,
Steuerbescheinigung) remain the source of truth. See ``docs/tax-cockpit.md``.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from dataclasses import replace
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    ActivityLedgerEntry,
    TaxLedgerEvent,
    TaxLot,
)
from app.foundation.settings import get_public_settings
from app.foundation.tax_calc import (
    Holding as HarvestHolding,
    OpenLot,
    apply_sparer_pauschbetrag,
    apply_teilfreistellung,
    cap_foreign_wht,
    classify_loss_bucket,
    fifo_consume,
    kirchensteuer_amount,
    reduced_kest_amount,
    soli_amount,
    teilfreistellung_pct_for_fund_class,
    vorabpauschale_estimate,
)
from app.foundation.tax_calc.jurisdictions.de.vorabpauschale import vorabpauschale_credit
from app.foundation.tax_calc.allowances import (
    KEST_RATE,
    SPARER_PAUSCHBETRAG_SINGLE,
    SPARER_PAUSCHBETRAG_SPOUSE,
)
from app.foundation.fund_class import (
    ClassificationSource,
    FundClass,
    classify_fund_class_with_source,
)

logger = logging.getLogger(__name__)


ESTIMATE_DISCLAIMER = (
    "Estimate only -- broker statements (Steuerbescheinigung / Erträgnisaufstellung) "
    "remain the source of truth. Not tax advice."
)


def _estimation_allowed(db: Session) -> tuple[bool, str | None]:
    """Check whether German tax estimation is allowed by current settings.

    Returns (True, None) if estimation may proceed, or (False, reason) if
    estimation is disabled because ``tax_estimation_enabled`` is off or
    ``tax_residency_country`` is not ``"DE"``.
    """
    s = _settings(db)
    enabled, reason = _estimation_enabled_from_settings(s)
    if not enabled:
        return False, reason
    if s.get("tax_residency_country") != "DE":
        return False, "German tax estimation requires tax_residency_country = 'DE'."
    return True, None


def _box3_estimation_allowed(db: Session) -> tuple[bool, str | None]:
    """Check whether Dutch Box 3 estimation is allowed by current settings."""
    s = _settings(db)
    enabled, reason = _estimation_enabled_from_settings(s)
    if not enabled:
        return False, reason
    if s.get("tax_residency_country") != "NL":
        return False, "Dutch Box 3 estimation requires tax_residency_country = 'NL'."
    return True, None


def _estimation_enabled_from_settings(settings: dict[str, Any]) -> tuple[bool, str | None]:
    if not settings.get("tax_estimation_enabled", False):
        return (
            False,
            "Tax estimation is disabled. Enable 'tax_estimation_enabled' in Control Center.",
        )
    return True, None


def _disabled_payload(reason: str | None) -> dict[str, Any]:
    """Return a standard disabled-estimation payload."""
    return {
        "estimate": True,
        "not_tax_advice": True,
        "disabled": True,
        "reason": reason or "Tax estimation is disabled.",
    }


def _settings(db: Session) -> dict[str, Any]:
    return get_public_settings(db)


def _allowance(db: Session, user_id: str) -> tuple[Decimal, Decimal]:
    settings = _settings(db)
    spouse = bool(settings.get("tax_spouse_allowance", False))
    default = SPARER_PAUSCHBETRAG_SPOUSE if spouse else SPARER_PAUSCHBETRAG_SINGLE
    amount = Decimal(str(settings.get("freistellungsauftrag_amount", default)))
    used = Decimal(str(settings.get("freistellungsauftrag_used", 0)))
    return amount, used


def _church_rate(db: Session) -> Decimal:
    raw = _settings(db).get("church_tax", "none")
    if raw in (None, "none", "", 0):
        return Decimal("0")
    try:
        rate = Decimal(str(raw))
    except Exception:
        return Decimal("0")
    if rate > 1:
        rate = rate / Decimal("100")
    return rate


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def list_lots(db: Session, user_id: str) -> list[TaxLot]:
    return (
        db.query(TaxLot)
        .filter(TaxLot.user_id == user_id)
        .order_by(TaxLot.isin, TaxLot.acquired_at)
        .all()
    )


def list_events(db: Session, user_id: str, year: int | None = None) -> list[TaxLedgerEvent]:
    q = db.query(TaxLedgerEvent).filter(TaxLedgerEvent.user_id == user_id)
    if year is not None:
        q = q.filter(TaxLedgerEvent.tax_year == year)
    return q.order_by(TaxLedgerEvent.event_date.desc()).all()


def _load_etf_universe_index() -> dict[str, dict[str, Any]]:
    """ISIN → ETF-record index from the LOCAL etf_universe cache only.

    Never triggers a live justETF fetch — same hang-vector reasoning as the
    cache-only pricing in :func:`_estimate_holdings_vorabpauschale`. Any
    failure degrades to an empty index plus a warning, so classification
    falls back to the conservative ``"other"`` floor.
    """
    try:
        from app.foundation.etf_universe import EtfUniverseProvider

        records = EtfUniverseProvider().get_universe_cached()
    except Exception as exc:
        logger.warning(
            "tax_cockpit: etf_universe lookup failed (%s); classifying without universe evidence",
            exc,
        )
        return {}
    return {r["isin"]: r for r in records if r.get("isin")}


def _safe_etf_index() -> dict[str, dict[str, Any]]:
    """Guarded wrapper: any loader failure degrades to an empty index + warning."""
    try:
        return _load_etf_universe_index()
    except Exception as exc:
        logger.warning(
            "tax_cockpit: etf_universe lookup failed (%s); classifying without universe evidence",
            exc,
        )
        return {}


def _classify_holding(
    *,
    user_fund_class: str | None,
    isin: str | None,
    etf_index: dict[str, dict[str, Any]] | None,
) -> tuple[FundClass, ClassificationSource]:
    """Unified evidence rule: user class → etf_universe match → 'other' floor."""
    holding: dict[str, Any] = {"fund_class": user_fund_class, "isin": isin}
    if user_fund_class is None and etf_index and isin:
        holding["etf_universe_match"] = etf_index.get(isin)
    return classify_fund_class_with_source(holding)


# Lots created by code, not typed in by the user.
_AUTO_LOT_SOURCES = frozenset({"activity-ledger", "scalable_sync"})


def _estimate_holdings_vorabpauschale(
    db: Session, user_id: str, year: int
) -> tuple[list[dict[str, Any]], Decimal]:
    """Best-effort Vorabpauschale across the user's fund/ETF holdings.

    Values each fund using year-start and year-end (or latest available) closes
    from the market-history cache, applies Teilfreistellung, and returns
    ``(line_items, total_taxable_eur)``. Returns ``([], 0)`` when there are no
    usable fund holdings or no cached prices. Every value is an estimate; broker
    statements remain the source of truth.
    """
    from app.foundation.models.entities import Holding, Portfolio
    from app.foundation.market import history as market_history

    def _year_of(value: Any) -> int | None:
        if hasattr(value, "year"):
            return value.year
        try:
            return int(str(value)[:4])
        except (ValueError, TypeError):
            return None

    portfolios = db.query(Portfolio).filter(Portfolio.user_id == user_id).all()
    holdings: list[Holding] = [h for p in portfolios for h in p.holdings]

    # Real fund classification per ISIN, sourced from the user's TaxLot rows
    # (most recently updated lot per ISIN wins when a fund was re-classified).
    fund_class_by_isin: dict[str, str] = {}
    # Earliest purchase per ISIN among the lots still open (the units held today):
    # bought after ``year`` they had no Vorabpauschale for it, bought during it
    # they are prorated. A position sold and bought back counts from the rebuy.
    first_acquired: dict[str, date] = {}
    lots = (
        db.query(TaxLot)
        .filter(TaxLot.user_id == user_id, TaxLot.isin.isnot(None))
        .order_by(TaxLot.isin, TaxLot.updated_at)
        .all()
    )
    for lot in lots:
        # A lot the sync created without fund evidence carries the "other"
        # floor; it is not the user's class and must not hide evidence the
        # etf_universe has for the ISIN.
        if lot.fund_class and not (lot.fund_class == "other" and lot.source in _AUTO_LOT_SOURCES):
            fund_class_by_isin[lot.isin] = lot.fund_class
        open_lot = lot.quantity_remaining is not None and lot.quantity_remaining > 0
        if open_lot and lot.acquired_at and (
            lot.isin not in first_acquired or lot.acquired_at < first_acquired[lot.isin]
        ):
            first_acquired[lot.isin] = lot.acquired_at

    # Distributions booked in the fund year, gross (before Teilfreistellung):
    # they reduce the Vorabpauschale of a distributing fund (Sec. 18(1) InvStG).
    distributions: dict[str, Decimal] = {}
    for ev in (
        db.query(TaxLedgerEvent)
        .filter(TaxLedgerEvent.user_id == user_id, TaxLedgerEvent.tax_year == year,
                TaxLedgerEvent.event_type == "dividend", TaxLedgerEvent.isin.isnot(None))
        .all()
    ):
        key = (ev.isin or "").upper()
        distributions[key] = distributions.get(key, Decimal("0")) + Decimal(str(ev.gross_eur or 0))

    line_items: list[dict[str, Any]] = []
    total_taxable = Decimal("0")

    for h in holdings:
        # Vorabpauschale only applies to investment funds (ETFs / mutual funds).
        if (h.asset_type or "").lower() not in ("etf", "fund"):
            continue
        qty = Decimal(str(h.quantity or 0))
        if qty <= 0:
            continue
        symbol = h.ticker or h.isin
        if not symbol:
            continue

        # Cache-only: this runs per-holding inside a request, so never trigger a
        # synchronous live provider fetch (a known hang vector). Estimates fall
        # back to whatever prices are already cached.
        rows = market_history(db, symbol, days=365 * 3 + 30, allow_live=False)
        closes = [(r["date"], r["close"]) for r in rows if r.get("close") is not None]
        in_year = [(d, c) for d, c in closes if _year_of(d) == year]
        if not in_year:
            continue

        start_price = Decimal(str(in_year[0][1]))
        end_price = Decimal(str(in_year[-1][1]))
        fund_value_start = qty * start_price
        fund_value_end = qty * end_price

        # Prorate when the position was opened during the year; skip it when it
        # was opened later. Positions sold since are not in the holdings at all,
        # so for a past year this can only understate (the bank's statement rules).
        acquired = h.buy_date or (first_acquired.get(h.isin) if h.isin else None)
        if acquired and acquired.year > year:
            continue
        months_held = 12
        if acquired and acquired.year == year:
            months_held = max(1, 12 - acquired.month + 1)

        # Unified evidence-based classification (todo 22): the user's own
        # TaxLot class for this ISIN wins; otherwise an etf_universe ISIN
        # match (cache-only) provides evidence; with neither, the conservative
        # "other" floor (0 % Teilfreistellung) applies — it can only overstate
        # estimated tax, never understate it.
        etf_index: dict[str, dict[str, Any]] | None = None
        user_class = fund_class_by_isin.get(h.isin) if h.isin else None
        if user_class is None:
            if etf_index is None:
                etf_index = _safe_etf_index()
            fund_class, class_source = _classify_holding(
                user_fund_class=None, isin=h.isin, etf_index=etf_index
            )
        else:
            fund_class, class_source = _classify_holding(
                user_fund_class=user_class, isin=h.isin, etf_index=None
            )
        raw = vorabpauschale_estimate(
            fund_value_start_of_year_eur=fund_value_start,
            fund_value_end_of_year_eur=fund_value_end,
            distributions_during_year_eur=distributions.get((h.isin or "").upper(), Decimal("0")),
            tax_year=year,
            months_held=months_held,
        )
        vorab = Decimal(str(raw["vorabpauschale_eur"]))
        if vorab <= 0:
            continue
        tf_pct = teilfreistellung_pct_for_fund_class(fund_class)
        taxable = apply_teilfreistellung(vorab, tf_pct)
        total_taxable += taxable
        line_items.append(
            {
                "isin": h.isin,
                "ticker": h.ticker,
                "name": h.name,
                "fund_class": fund_class,
                "classification": {"value": fund_class, "source": class_source},
                "teilfreistellung_pct": float(tf_pct),
                "fund_value_start_eur": float(fund_value_start),
                "fund_value_end_eur": float(fund_value_end),
                "distributions_eur": raw["distributions_eur"],
                "basiszins": raw["basiszins"],
                "basiszins_known": raw["basiszins_known"],
                "months_held": months_held,
                "vorabpauschale_eur": float(vorab),
                "taxable_after_teilfreistellung_eur": float(taxable),
            }
        )

    return line_items, total_taxable


def compute_year_overview(db: Session, user_id: str, year: int) -> dict[str, Any]:
    """Aggregate KAP-style overview for ``year``.

    Returns a payload tagged ``estimate`` everywhere with line-item provenance.
    """
    allowed, reason = _estimation_allowed(db)
    if not allowed:
        return _disabled_payload(reason)
    events = list_events(db, user_id, year)

    dividends_gross = Decimal("0")
    dividends_taxable = Decimal("0")
    interest_gross = Decimal("0")
    realised_gain_aktien = Decimal("0")
    realised_loss_aktien = Decimal("0")
    realised_gain_sonstige = Decimal("0")
    realised_loss_sonstige = Decimal("0")
    vorabpauschale_taxable = Decimal("0")
    has_vorab_events = False
    foreign_wht_creditable = Decimal("0")
    foreign_wht_excess = Decimal("0")
    kest_paid = Decimal("0")
    sources: dict[str, int] = {}

    for ev in events:
        sources[ev.source] = sources.get(ev.source, 0) + 1
        gross = Decimal(str(ev.gross_eur or 0))
        tf_pct = Decimal(str(ev.teilfreistellung_pct or 0))

        if ev.event_type == "dividend":
            dividends_gross += gross
            taxable = apply_teilfreistellung(gross, tf_pct)
            dividends_taxable += taxable
            if ev.foreign_wht_eur and Decimal(str(ev.foreign_wht_eur)) > 0:
                wht = cap_foreign_wht(
                    Decimal(str(ev.foreign_wht_eur)),
                    gross,
                    country=ev.foreign_country,
                )
                foreign_wht_creditable += Decimal(str(wht["creditable_eur"]))
                foreign_wht_excess += Decimal(str(wht["excess_eur"]))
        elif ev.event_type == "interest":
            interest_gross += gross
        elif ev.event_type == "sale":
            realised = Decimal(str(ev.realised_gain_eur or 0))
            # §20(4) InvStG 2018: Teilfreistellung applies symmetrically to both
            # gains AND losses -- fund losses are reduced by the exempt fraction
            # before offsetting gains. Exception: pre-2018-holdings (§20 Abs.1
            # Satz 1 InvStG, BFH VIII R 15/22, Nov 2025) where the fictional
            # acquisition cost (1.1.2018 price) exceeds the historical cost --
            # Teilfreistellung does not apply to the loss in that case. We cannot
            # detect this without per-lot acquisition_date < 2018 data; add a
            # TODO when that information becomes available in TaxLot.
            taxable_realised = apply_teilfreistellung(realised, tf_pct)
            bucket = (ev.bucket or "sonstige").lower()
            if bucket == "aktien":
                if taxable_realised >= 0:
                    realised_gain_aktien += taxable_realised
                else:
                    realised_loss_aktien += -taxable_realised
            else:
                if taxable_realised >= 0:
                    realised_gain_sonstige += taxable_realised
                else:
                    realised_loss_sonstige += -taxable_realised
        elif ev.event_type == "vorabpauschale":
            has_vorab_events = True
            vorabpauschale_taxable += apply_teilfreistellung(gross, tf_pct)
        elif ev.event_type == "withholding":
            kest_paid += Decimal(str(ev.withheld_eur or 0))

    # Auto-estimate Vorabpauschale from current fund/ETF holdings when the user has
    # no explicit vorabpauschale events for the year (avoids double counting). Gate
    # on the *presence* of explicit events, not their taxable sum — events can
    # legitimately net to zero (e.g. after Teilfreistellung) yet still represent
    # authoritative coverage that must not be overridden by an estimate. This is a
    # best-effort estimate; the broker's Steuerbescheinigung is authoritative.
    #
    # Sec. 18(3) InvStG: the Vorabpauschale for a calendar year is deemed received
    # on the first working day of the next one, so the tax year ``year`` carries
    # the one computed for ``year - 1`` (Basiszins and fund performance of that
    # year), booked by the bank in January against this year's allowance.
    vorab_estimated_items: list[dict[str, Any]] = []
    vorab_estimated_total = Decimal("0")
    if not has_vorab_events:
        vorab_estimated_items, vorab_estimated_total = _estimate_holdings_vorabpauschale(
            db, user_id, year - 1
        )
        vorabpauschale_taxable += vorab_estimated_total

    # §20 Abs. 6 EStG loss offsetting.
    #
    # Satz 4 ring-fences share losses: Verluste aus der Veraeusserung von Aktien
    # may be offset against Gewinne aus der Veraeusserung von Aktien and nothing
    # else. This is the only ring-fence in the provision.
    net_aktien = max(Decimal("0"), realised_gain_aktien - realised_loss_aktien)
    aktien_loss_carryforward = max(Decimal("0"), realised_loss_aktien - realised_gain_aktien)

    # Saetze 1-3 create a single general pot for every other loss (funds, bonds,
    # derivatives). Satz 1 bars offsetting only against *other Einkunftsarten* --
    # it does not partition income within Einkuenfte aus Kapitalvermoegen. So the
    # general pot offsets everything else in the category: sonstige gains, net
    # Aktien gains, dividends, interest and Vorabpauschale alike.
    #
    # (Saetze 5-6 -- the separate Termingeschaefte loss circle and its EUR 20,000
    # annual cap -- were repealed by the Jahressteuergesetz 2024, retroactively
    # for 2024 and all open cases per Sec. 52 Abs. 28 EStG, so derivative losses
    # land in this same general pot with no cap.)
    #
    # The previous implementation clamped the general pot to sonstige gains only,
    # which over-stated taxable income for every user who realised a fund or bond
    # loss in a year with dividend or interest income -- always against the user.
    income_before_general_losses = max(
        Decimal("0"),
        dividends_taxable
        + interest_gross
        + net_aktien
        + realised_gain_sonstige
        + vorabpauschale_taxable,
    )
    general_loss_offset = min(realised_loss_sonstige, income_before_general_losses)
    sonstige_loss_carryforward = realised_loss_sonstige - general_loss_offset

    taxable_income = income_before_general_losses - general_loss_offset

    # Retained for the response contract: the sonstige bucket's own net result.
    # It no longer feeds taxable_income -- the general pot above does.
    net_sonstige_gain = max(Decimal("0"), realised_gain_sonstige - realised_loss_sonstige)

    allowance_total, allowance_used = _allowance(db, user_id)
    taxable_after_allowance, used_now = apply_sparer_pauschbetrag(
        taxable_income, allowance_total, allowance_used
    )

    kest_due_gross = (taxable_after_allowance * KEST_RATE).quantize(Decimal("0.01"))
    church_rate = _church_rate(db)
    if church_rate > 0:
        # §32d(1) EStG: church tax is deductible, lowering the effective KESt.
        # The closed form folds in the foreign-WHT credit (issue #140).
        kest_after_foreign = reduced_kest_amount(
            taxable_after_allowance, foreign_wht_creditable, church_rate
        )
    else:
        # Foreign WHT is creditable against KESt.
        kest_after_foreign = max(Decimal("0"), kest_due_gross - foreign_wht_creditable)
    soli = soli_amount(kest_after_foreign)
    church = kirchensteuer_amount(kest_after_foreign, church_rate)
    total_tax_due = kest_after_foreign + soli + church
    settlement = total_tax_due - kest_paid  # positive: owe, negative: refund

    return {
        "tax_year": year,
        "label": ESTIMATE_DISCLAIMER,
        "estimate": True,
        "not_tax_advice": True,
        "allowance": {
            "amount_eur": float(allowance_total),
            "used_before_eur": float(allowance_used),
            "used_now_eur": float(used_now),
            "remaining_after_eur": float(
                max(Decimal("0"), allowance_total - allowance_used - used_now)
            ),
        },
        "lines": {
            "dividends_gross_eur": float(dividends_gross),
            "dividends_taxable_eur": float(dividends_taxable),
            "interest_gross_eur": float(interest_gross),
            "realised_gain_aktien_eur": float(realised_gain_aktien),
            "realised_loss_aktien_eur": float(realised_loss_aktien),
            "realised_gain_sonstige_eur": float(realised_gain_sonstige),
            "realised_loss_sonstige_eur": float(realised_loss_sonstige),
            "net_aktien_eur": float(net_aktien),
            "net_sonstige_gain_eur": float(net_sonstige_gain),
            "general_loss_offset_eur": float(general_loss_offset),
            "vorabpauschale_taxable_eur": float(vorabpauschale_taxable),
            "foreign_wht_creditable_eur": float(foreign_wht_creditable),
            "foreign_wht_excess_eur": float(foreign_wht_excess),
            "taxable_income_before_allowance_eur": float(taxable_income),
            "taxable_income_after_allowance_eur": float(taxable_after_allowance),
        },
        "tax": {
            "kest_rate": float(KEST_RATE),
            "kest_due_eur": float(kest_due_gross),
            "kest_after_foreign_wht_eur": float(kest_after_foreign),
            "soli_eur": float(soli),
            "church_tax_rate": float(church_rate),
            "church_tax_eur": float(church),
            "total_tax_due_eur": float(total_tax_due),
            "kest_already_paid_eur": float(kest_paid),
            "settlement_eur": float(settlement),
        },
        "carryforward": {
            "aktien_loss_eur": float(aktien_loss_carryforward),
            "sonstige_loss_eur": float(sonstige_loss_carryforward),
        },
        "anlage_kap_mapping": _kap_mapping(
            computation_year=year,
            dividends_gross=dividends_gross,
            interest=interest_gross,
            sales_net=net_aktien + net_sonstige_gain,
            vorab=vorabpauschale_taxable,
            kest_paid=kest_paid,
            foreign_wht=foreign_wht_creditable,
        ),
        "vorabpauschale_estimate": {
            "estimated_from_holdings": bool(vorab_estimated_items),
            "taxable_eur": float(vorab_estimated_total),
            # Computed for this fund year; received (and taxed) on the first
            # working day of ``tax_year``.
            "fund_year": year - 1,
            "items": vorab_estimated_items,
        },
        "provenance": {
            "events_by_source": sources,
            "events_total": len(events),
        },
    }


def _kap_mapping(
    *,
    computation_year: int,
    dividends_gross: Decimal,
    interest: Decimal,
    sales_net: Decimal,
    vorab: Decimal,
    kest_paid: Decimal,
    foreign_wht: Decimal,
) -> dict[str, Any]:
    """Approximate Anlage-KAP line numbers. Estimates only."""
    return {
        "form": "Anlage KAP",
        "year_hint": (
            f"{computation_year}-form layout (Anlage KAP-INV) — "
            "verify against the current year's form."
        ),
        "line_7_kapitalertraege": float(dividends_gross + interest + sales_net + vorab),
        "line_8_gewinne_aktien": float(sales_net),
        "line_19_vorabpauschale": float(vorab),
        "line_37_kest_paid": float(kest_paid),
        "line_41_anrechenbare_auslaendische_steuer": float(foreign_wht),
    }


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


# Synced events name their bank through their source; each bank applies only
# its own Freistellungsauftrag (Sec. 44a EStG).
SOURCE_INSTITUTION = {"dkb_sync": "dkb", "scalable_sync": "scalable"}


def normalise_institution(value: Any) -> str | None:
    text = " ".join(str(value or "").split()).lower()[:32]
    return text or None


def event_institution(event: TaxLedgerEvent) -> str | None:
    return event.institution or SOURCE_INSTITUTION.get(event.source or "")


def create_event(db: Session, user_id: str, data: dict[str, Any]) -> TaxLedgerEvent:
    event = TaxLedgerEvent(
        user_id=user_id,
        tax_year=int(data.get("tax_year") or _year_of(data.get("event_date"))),
        event_date=_as_date(data.get("event_date")),
        event_type=str(data.get("event_type", "dividend")),
        isin=data.get("isin"),
        symbol=data.get("symbol"),
        name=data.get("name"),
        fund_class=data.get("fund_class"),
        teilfreistellung_pct=Decimal(
            str(
                data.get("teilfreistellung_pct")
                if data.get("teilfreistellung_pct") is not None
                else teilfreistellung_pct_for_fund_class(data.get("fund_class"))
            )
        ),
        gross_eur=Decimal(str(data.get("gross_eur", 0))),
        withheld_eur=Decimal(str(data.get("withheld_eur", 0))),
        foreign_wht_eur=Decimal(str(data.get("foreign_wht_eur", 0))),
        foreign_country=data.get("foreign_country"),
        realised_gain_eur=(
            Decimal(str(data.get("realised_gain_eur")))
            if data.get("realised_gain_eur") is not None
            else None
        ),
        bucket=data.get("bucket")
        or (
            classify_loss_bucket(data.get("asset_type"), data.get("fund_class"))
            if data.get("event_type") == "sale"
            else None
        ),
        confidence=data.get("confidence", "estimate"),
        source=data.get("source", "manual"),
        source_ref=data.get("source_ref"),
        institution=normalise_institution(data.get("institution"))
        or SOURCE_INSTITUTION.get(str(data.get("source") or "")),
        notes=data.get("notes"),
        payload_json=json.dumps(data.get("payload", {})),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def create_lot(db: Session, user_id: str, data: dict[str, Any]) -> TaxLot:
    qty = Decimal(str(data["quantity"]))
    lot = TaxLot(
        user_id=user_id,
        isin=data["isin"],
        symbol=data.get("symbol"),
        name=data.get("name"),
        account_ref=data.get("account_ref"),
        fund_class=data.get("fund_class", "other"),
        teilfreistellung_pct=Decimal(
            str(
                data.get("teilfreistellung_pct")
                if data.get("teilfreistellung_pct") is not None
                else teilfreistellung_pct_for_fund_class(data.get("fund_class"))
            )
        ),
        acquired_at=_as_date(data["acquired_at"]),
        quantity_initial=qty,
        quantity_remaining=qty,
        cost_basis_eur=Decimal(str(data["cost_basis_eur"])),
        fees_eur=Decimal(str(data.get("fees_eur", 0))),
        fx_rate=(Decimal(str(data.get("fx_rate"))) if data.get("fx_rate") is not None else None),
        source=data.get("source", "manual"),
        source_ref=data.get("source_ref"),
        notes=data.get("notes"),
    )
    db.add(lot)
    db.commit()
    db.refresh(lot)
    return lot


def _vorab_per_unit_by_year(db: Session, symbol: str, until_year: int) -> dict[int, Decimal]:
    """Full-year Vorabpauschale per unit of ``symbol`` for each fund year with cached prices.

    Cache-only EUR closes; distributions are taken as nil (accumulating
    funds), which can only overstate the credit of a distributing fund.
    Computed on 10,000 units so rounding to the cent does not bias it.
    """
    from app.foundation.eur_prices import eur_closes

    closes = eur_closes(db, symbol, days=(date.today() - date(2018, 1, 1)).days, allow_live=False)
    by_year: dict[int, list[tuple[str, float]]] = {}
    for day, close in sorted(closes.items()):
        by_year.setdefault(int(day[:4]), []).append((day, close))
    scale = Decimal("10000")
    out: dict[int, Decimal] = {}
    for year, rows in by_year.items():
        if year < 2018 or year >= until_year or len(rows) < 2:
            continue
        raw = vorabpauschale_estimate(
            fund_value_start_of_year_eur=Decimal(str(rows[0][1])) * scale,
            fund_value_end_of_year_eur=Decimal(str(rows[-1][1])) * scale,
            distributions_during_year_eur=Decimal("0"),
            tax_year=year,
        )
        out[year] = Decimal(str(raw["vorabpauschale_eur"])) / scale
    return out


def record_sale_via_fifo(
    db: Session,
    user_id: str,
    *,
    isin: str,
    sell_date: date,
    sell_quantity: Decimal,
    sell_price_per_unit_eur: Decimal,
    sell_fees_eur: Decimal = Decimal("0"),
    fund_class: str | None = None,
    bucket: str | None = None,
    source: str = "manual",
    source_ref: str | None = None,
    account_ref: str | None = None,
    institution: str | None = None,
    acquired_on_or_before: date | None = None,
) -> TaxLedgerEvent:
    """Consume open lots for ``isin`` FIFO and record a ``sale`` TaxLedgerEvent.

    ``account_ref`` keeps FIFO inside one depot; ``acquired_on_or_before``
    keeps a sale replayed from history off lots bought after it.
    """
    query = db.query(TaxLot).filter(
        TaxLot.user_id == user_id, TaxLot.isin == isin, TaxLot.quantity_remaining > 0
    )
    if account_ref:
        query = query.filter(TaxLot.account_ref == account_ref)
    if acquired_on_or_before is not None:
        query = query.filter(TaxLot.acquired_at <= acquired_on_or_before)
    lots_rows = query.order_by(TaxLot.acquired_at).all()
    lots_view: list[dict] = [
        {
            "lot_id": row.id,
            "acquired_at": row.acquired_at,
            "quantity_remaining": row.quantity_remaining,
            "cost_basis_eur": row.cost_basis_eur,
            "fund_class": row.fund_class,
        }
        for row in lots_rows
    ]
    sale = fifo_consume(lots_view, sell_quantity, sell_price_per_unit_eur, sell_fees_eur)

    # Persist mutations
    row_by_id = {r.id: r for r in lots_rows}
    for view in lots_view:
        row = row_by_id[view["lot_id"]]
        row.quantity_remaining = view["quantity_remaining"]
        row.cost_basis_eur = view["cost_basis_eur"]
        if row.quantity_remaining <= 0:
            row.closed_at = sell_date

    resolved_fund_class = fund_class or (lots_rows[0].fund_class if lots_rows else None)
    tf_pct = teilfreistellung_pct_for_fund_class(resolved_fund_class)
    # Sec. 19(1) InvStG: Vorabpauschalen taxed while the units were held come
    # off the gain, or they would be taxed twice.
    vorab_credit = Decimal("0")
    symbol = next((r.symbol for r in lots_rows if r.symbol), None)
    if resolved_fund_class is not None and symbol and sale.consumed_lots:
        per_unit = _vorab_per_unit_by_year(db, symbol, sell_date.year)
        for slice_ in sale.consumed_lots:
            vorab_credit += vorabpauschale_credit(
                acquired_at=_as_date(slice_["acquired_at"]),
                sold_at=sell_date,
                quantity=Decimal(slice_["quantity_taken"]),
                per_unit_by_year=per_unit,
            )
    event = TaxLedgerEvent(
        user_id=user_id,
        tax_year=sell_date.year,
        event_date=sell_date,
        event_type="sale",
        isin=isin,
        name=lots_rows[0].name if lots_rows else None,
        fund_class=resolved_fund_class,
        teilfreistellung_pct=tf_pct,
        gross_eur=sale.proceeds_eur,
        realised_gain_eur=sale.realised_gain_eur - vorab_credit,
        bucket=bucket
        or classify_loss_bucket(
            "stock" if resolved_fund_class is None else "fund", resolved_fund_class
        ),
        confidence="estimate",
        source=source,
        source_ref=source_ref,
        institution=normalise_institution(institution) or SOURCE_INSTITUTION.get(source),
        payload_json=json.dumps(
            {
                "quantity_sold": str(sale.quantity_sold),
                "cost_basis_eur": str(sale.cost_basis_eur),
                "fees_eur": str(sell_fees_eur),
                "consumed_lots": sale.consumed_lots,
                "gain_before_vorabpauschale_credit_eur": str(sale.realised_gain_eur),
                "vorabpauschale_credit_eur": str(vorab_credit),
            }
        ),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def estimate_vorabpauschale(
    db: Session | None = None,
    *,
    isin: str,
    fund_value_start_of_year_eur: Decimal,
    fund_value_end_of_year_eur: Decimal,
    distributions_during_year_eur: Decimal,
    tax_year: int,
    fund_class: str | None = None,
    months_held: int = 12,
) -> dict[str, Any]:
    if db is not None:
        allowed, reason = _estimation_allowed(db)
        if not allowed:
            return _disabled_payload(reason)
    raw = vorabpauschale_estimate(
        fund_value_start_of_year_eur=fund_value_start_of_year_eur,
        fund_value_end_of_year_eur=fund_value_end_of_year_eur,
        distributions_during_year_eur=distributions_during_year_eur,
        tax_year=tax_year,
        months_held=months_held,
    )
    # Same unified evidence rule as the holdings estimator: an explicitly
    # passed fund_class is user evidence; without one, a cache-only
    # etf_universe ISIN match may supply it; else the "other" floor.
    etf_index: dict[str, dict[str, Any]] | None = None
    if fund_class is None:
        etf_index = _safe_etf_index()
    fund_class, class_source = _classify_holding(
        user_fund_class=fund_class, isin=isin, etf_index=etf_index
    )
    tf_pct = teilfreistellung_pct_for_fund_class(fund_class)
    taxable = apply_teilfreistellung(Decimal(str(raw["vorabpauschale_eur"])), tf_pct)
    return {
        **raw,
        "isin": isin,
        "fund_class": fund_class,
        "classification": {"value": fund_class, "source": class_source},
        "teilfreistellung_pct": float(tf_pct),
        "taxable_after_teilfreistellung_eur": float(taxable),
        "estimate": True,
        "not_tax_advice": True,
    }


# ---------------------------------------------------------------------------
# Tax settings (Control Center)
# ---------------------------------------------------------------------------


TAX_SETTING_KEYS = (
    "tax_residency_country",
    "church_tax",
    "freistellungsauftrag_amount",
    "freistellungsauftrag_used",
    "freistellungsauftrag_dkb_eur",
    "freistellungsauftrag_scalable_eur",
    "freistellungsauftrag_other_banks_eur",
    "tax_interest_rate_dkb",
    "tax_spouse_allowance",
    "tax_estimation_enabled",
    "tax_nv_certificate",
    "tax_nv_valid_until",
    "tax_nv_filed_dkb",
    "tax_nv_filed_scalable",
    "tax_other_income_eur",
    "tax_health_insurance",
    "tax_bafoeg",
)


def get_tax_settings(db: Session) -> dict[str, Any]:
    s = _settings(db)
    return {key: s.get(key) for key in TAX_SETTING_KEYS}


def list_lots_with_provenance(db: Session, user_id: str) -> list[dict[str, Any]]:
    lots = list_lots(db, user_id)
    out = []
    for lot in lots:
        out.append(
            {
                "id": lot.id,
                "isin": lot.isin,
                "symbol": lot.symbol,
                "name": lot.name,
                "fund_class": lot.fund_class,
                "teilfreistellung_pct": float(lot.teilfreistellung_pct),
                "acquired_at": lot.acquired_at.isoformat(),
                "quantity_initial": float(lot.quantity_initial),
                "quantity_remaining": float(lot.quantity_remaining),
                "cost_basis_eur": float(lot.cost_basis_eur),
                "fees_eur": float(lot.fees_eur),
                "source": lot.source,
                "source_ref": lot.source_ref,
                "closed_at": lot.closed_at.isoformat() if lot.closed_at else None,
                "confidence": "estimate",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Heuristic seeding from activity ledger (best-effort)
# ---------------------------------------------------------------------------


def seed_lots_from_activity(db: Session, user_id: str) -> int:
    """Create TaxLots from BUY entries in the activity ledger that don't yet have a lot.

    Best-effort. Marks every created lot with ``source='activity-ledger'`` and
    ``confidence='estimate'`` via ``source_ref``.
    """
    activities = (
        db.query(ActivityLedgerEntry)
        .filter(
            ActivityLedgerEntry.user_id == user_id,
            ActivityLedgerEntry.activity_type == "buy",
            ActivityLedgerEntry.isin.is_not(None),
        )
        .all()
    )
    existing_refs = {
        lot.source_ref
        for lot in db.query(TaxLot.source_ref).filter(TaxLot.user_id == user_id).all()
        if lot.source_ref
    }
    created = 0
    etf_index: dict[str, dict[str, Any]] | None = None
    for entry in activities:
        ref = f"activity:{entry.id}"
        if ref in existing_refs:
            continue
        qty = Decimal(str(entry.quantity or 0))
        if qty <= 0:
            continue
        price = Decimal(str(entry.price or 0))
        fees = Decimal(str(entry.fees or 0))
        cost = (qty * price) + fees
        if etf_index is None:
            etf_index = _safe_etf_index()
        # Evidence only (etf_universe match); without it the "other" floor.
        fund_class, _ = _classify_holding(user_fund_class=None, isin=entry.isin, etf_index=etf_index)
        lot = TaxLot(
            user_id=user_id,
            isin=entry.isin,
            symbol=entry.symbol,
            name=entry.description[:200] if entry.description else None,
            account_ref=entry.connected_account_id,
            fund_class=fund_class,
            teilfreistellung_pct=teilfreistellung_pct_for_fund_class(fund_class),
            acquired_at=entry.date,
            quantity_initial=qty,
            quantity_remaining=qty,
            cost_basis_eur=cost,
            fees_eur=fees,
            source="activity-ledger",
            source_ref=ref,
        )
        db.add(lot)
        created += 1
    if created:
        db.commit()
    return created


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_date(value):
    if isinstance(value, date):
        return value
    if value is None:
        return date.today()
    return date.fromisoformat(str(value))


def _year_of(value) -> int:
    if value is None:
        return date.today().year
    if isinstance(value, date):
        return value.year
    return date.fromisoformat(str(value)).year


# ---------------------------------------------------------------------------
# Multi-jurisdiction support (Phase 7)
# ---------------------------------------------------------------------------


def get_user_jurisdiction(db: Session, user_id: str, year: int) -> str:
    """Get the primary tax jurisdiction for a user in a given year.

    Checks tax_residency_periods table for entries that overlap the year.
    If no explicit residency period is set, defaults to the public setting
    'default_tax_jurisdiction' (DE default).

    Args:
        db: Database session.
        user_id: User ID.
        year: Tax year.

    Returns:
        Two-letter country code: "DE" (default) or "NL".
    """
    from app.foundation.models.entities import TaxResidencyPeriod

    # Check for explicit residency period overlapping the tax year
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)

    periods = (
        db.query(TaxResidencyPeriod)
        .filter(
            TaxResidencyPeriod.user_id == user_id,
            TaxResidencyPeriod.valid_from <= year_end,
        )
        .all()
    )

    for period in periods:
        if period.valid_to is None or period.valid_to >= year_start:
            # This period overlaps with the tax year
            return period.country.upper()

    # No explicit residency, fall back to the Control Center tax residency.
    settings = _settings(db)
    default = settings.get("tax_residency_country") or settings.get(
        "default_tax_jurisdiction", "DE"
    )
    return default.upper()


def compute_box3_year_overview(
    db: Session, user_id: str, year: int, holdings: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Compute NL Box 3 (wealth tax) overview for a given year.

    Args:
        db: Database session.
        user_id: User ID.
        year: Tax year.
        holdings: List of holdings with 'amount_eur', 'type', 'currency' keys.
                  If not provided, assumes zero wealth.

    Returns:
        Dictionary with Box 3 calculation details.
    """
    allowed, reason = _box3_estimation_allowed(db)
    if not allowed:
        return _disabled_payload(reason)

    from app.foundation.tax_calc.jurisdictions.nl import calculate_box3

    if holdings is None:
        holdings = []

    total_wealth_eur = Decimal(str(sum(h.get("amount_eur", 0) for h in holdings)))

    # Check for dual residency (partial-year residency in NL)
    days_resident_nl = None
    from app.foundation.models.entities import TaxResidencyPeriod

    residency = (
        db.query(TaxResidencyPeriod)
        .filter(
            TaxResidencyPeriod.user_id == user_id,
            TaxResidencyPeriod.country == "NL",
        )
        .order_by(TaxResidencyPeriod.valid_from.desc())
        .first()
    )

    if residency and residency.valid_to is None:
        # Full year in NL (no end date)
        days_resident_nl = 365
    elif residency:
        # Partial year: count days from valid_from to valid_to (or end of year)
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        from_date = max(residency.valid_from, year_start)
        to_date = min(residency.valid_to or year_end, year_end)
        if from_date <= to_date:
            days_resident_nl = (to_date - from_date).days + 1

    # Classify holdings into the three Box 3 buckets
    from app.foundation.tax_calc.jurisdictions.nl import classify_wealth_by_bucket

    bank_deposits_eur, investments_eur, debts_eur = classify_wealth_by_bucket(holdings)

    # Calculate Box 3 using the reformed three-bucket model
    calc = calculate_box3(
        total_wealth_eur=total_wealth_eur,
        year=year,
        days_resident_nl=days_resident_nl,
        bank_deposits_eur=bank_deposits_eur,
        investments_eur=investments_eur,
        debts_eur=debts_eur,
    )

    return {
        "tax_year": year,
        "jurisdiction": "NL",
        "label": "NL Box 3 (wealth tax) estimate -- always verify with your accountant.",
        "estimate": True,
        "not_tax_advice": True,
        "wealth": {
            "total_eur": float(calc.total_wealth_eur),
            "bank_deposits_eur": float(calc.bank_deposits_eur),
            "investments_eur": float(calc.investments_eur),
            "debts_eur": float(calc.debts_eur),
            "tax_free_allowance_eur": float(calc.heffingvrij_vermogen_eur),
            "taxable_eur": float(calc.taxable_wealth_eur),
            "is_prorated": calc.is_prorated,
            "days_resident_nl": calc.days_resident_nl,
        },
        "calculation": {
            "bank_rate_pct": float(calc.bank_rate_pct),
            "investment_rate_pct": float(calc.investment_rate_pct),
            "debt_rate_pct": float(calc.debt_rate_pct),
            "bank_fictitious_return_eur": float(calc.bank_fictitious_return_eur),
            "investment_fictitious_return_eur": float(calc.investment_fictitious_return_eur),
            "debt_deduction_eur": float(calc.debt_deduction_eur),
            "fictitious_return_rate_pct": float(calc.fictitious_return_rate_pct),
            "fictitious_return_eur": float(calc.fictitious_return_eur),
            "box3_tax_eur": float(calc.box3_tax_eur),
            "effective_tax_rate_pct": float(calc.effective_tax_rate_pct),
        },
    }


def compute_year_overview_by_jurisdiction(
    db: Session, user_id: str, year: int, jurisdiction: str | None = None
) -> dict[str, Any]:
    """Compute tax overview for a given jurisdiction.

    Routes to the appropriate calculator (DE or NL) based on the jurisdiction parameter
    or the user's residence.

    Args:
        db: Database session.
        user_id: User ID.
        year: Tax year.
        jurisdiction: Explicit jurisdiction override ("DE" or "NL"). If None, auto-detect.

    Returns:
        Tax calculation dictionary tagged with 'estimate' and 'not_tax_advice'.
    """
    if jurisdiction is None:
        jurisdiction = get_user_jurisdiction(db, user_id, year)

    jurisdiction = jurisdiction.upper()

    if jurisdiction == "NL":
        return compute_box3_year_overview(db, user_id, year)
    if jurisdiction == "DE":
        # German calculation is additionally gated on DE residency.
        allowed, reason = _estimation_allowed(db)
        if not allowed:
            return _disabled_payload(reason)
        return compute_year_overview(db, user_id, year)
    return _disabled_payload(
        f"Tax estimation is not supported for tax_residency_country = '{jurisdiction}'."
    )


# ---------------------------------------------------------------------------
# Tax-free gain harvesting (Nichtveranlagungsbescheinigung)
# ---------------------------------------------------------------------------

# DKB's order fee per notional: decision/advisor/costs.py DEFAULT_FEE_SCHEDULE,
# restated because foundation may not import the decision layer.
HARVEST_FEE_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("5000"), Decimal("10")),
    (Decimal("20000"), Decimal("15")),
)
HARVEST_FEE_ABOVE = Decimal("30")
# Half-spread per trade on an ETF on Xetra/Tradegate.
HARVEST_SPREAD_BPS = Decimal("10")
# Lots and the depot must agree this closely before a partial FIFO sale is priced.
_LOTS_MATCH_TOLERANCE = Decimal("0.005")
NV_EXPIRY_WARNING_DAYS = 60


# Brokers with one flat fee per order (decision/monthly_plan.py keeps the same
# price list): Scalable Capital FREE broker, 0.99 EUR on its European venue.
_FLAT_ORDER_FEE_EUR = {"scalable": Decimal("0.99")}


def _harvest_order_fee(notional: Decimal) -> Decimal:
    for bound, fee in HARVEST_FEE_TIERS:
        if notional <= bound:
            return fee
    return HARVEST_FEE_ABOVE


def _depot_positions(db: Session, user_id: str) -> list[Any]:
    """Every synced depot position (DKB and Scalable Capital), one entry per broker.

    FIFO applies per depot, so an ISIN held at two brokers stays two entries,
    and ``_harvest_holdings`` gives each only the lots booked to its own depot.
    """
    from app.foundation.live_positions import live_positions

    return live_positions(db, user_id)


def _harvest_holdings(
    db: Session, user_id: str,
) -> tuple[list[HarvestHolding], list[dict[str, Any]], list[dict[str, str]]]:
    """Depot positions with their FIFO lots, or an average-cost stand-in."""
    lots_by_isin: dict[str, list[TaxLot]] = {}
    for lot in list_lots(db, user_id):
        if lot.quantity_remaining and lot.quantity_remaining > 0:
            lots_by_isin.setdefault((lot.isin or "").upper(), []).append(lot)
    etf_index = _safe_etf_index()
    holdings: list[HarvestHolding] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    positions = _depot_positions(db, user_id)
    depots_per_isin: dict[str, int] = {}
    for pos in positions:
        key = (pos.isin or "").upper()
        depots_per_isin[key] = depots_per_isin.get(key, 0) + 1
    for pos in positions:
        source = getattr(pos, "source", "dkb")
        order_fee = _FLAT_ORDER_FEE_EUR.get(source)
        if source != "dkb":
            # Say where to sell: the harvest is a sale at that broker.
            pos = replace(pos, name=f"{pos.name} · {pos.broker_label}")
        isin = (pos.isin or "").upper()
        qty = Decimal(pos.quantity or 0)
        if qty <= 0:
            continue
        if pos.current_price is None:
            skipped.append({"isin": isin, "name": pos.name, "reason": "no current price from the depot sync"})
            continue
        price = Decimal(pos.current_price)
        lots = lots_by_isin.get(isin, [])
        if depots_per_isin.get(isin, 0) > 1:
            # FIFO is per depot: with the ISIN at two brokers, only lots booked to
            # this depot are its lots (none matching -> the average-cost stand-in).
            lots = [lot for lot in lots if lot.account_ref and lot.account_ref == getattr(pos, "account_id", None)]
        lot_qty = sum((Decimal(lot.quantity_remaining) for lot in lots), Decimal("0"))
        if lots and abs(lot_qty - qty) <= qty * _LOTS_MATCH_TOLERANCE:
            holdings.append(HarvestHolding(
                isin=isin, name=pos.name, price_eur=price,
                teilfreistellung_pct=Decimal(lots[0].teilfreistellung_pct or 0),
                lots=tuple(
                    OpenLot(lot.acquired_at, Decimal(lot.quantity_remaining), Decimal(lot.cost_basis_eur))
                    for lot in lots
                ),
                order_fee_eur=order_fee,
                bank=source,
            ))
            continue
        if pos.avg_buy_price is None:
            skipped.append({"isin": isin, "name": pos.name, "reason": "no tax lots and no average buy price"})
            continue
        if lots:
            warnings.append({
                "code": "lots_mismatch",
                "message": f"The tax lots for {pos.name or isin} do not match the depot quantity; "
                "only a sale of the whole position is suggested for it.",
            })
        fund_class, _ = _classify_holding(user_fund_class=None, isin=isin, etf_index=etf_index)
        holdings.append(HarvestHolding(
            isin=isin, name=pos.name, price_eur=price,
            teilfreistellung_pct=teilfreistellung_pct_for_fund_class(fund_class),
            lots=(OpenLot(date.min, qty, qty * Decimal(pos.avg_buy_price)),),
            average_cost_only=True,
            order_fee_eur=order_fee,
            bank=source,
        ))
    return holdings, skipped, warnings


def _nv_warnings(settings: dict[str, Any], today: date) -> tuple[list[dict[str, str]], dict[str, Any]]:
    warnings: list[dict[str, str]] = []
    valid_until = None
    raw = settings.get("tax_nv_valid_until")
    if raw:
        try:
            valid_until = date.fromisoformat(str(raw)[:10])
        except ValueError:
            valid_until = None
    days = (valid_until - today).days if valid_until else None
    if days is not None and days < 0:
        warnings.append({"code": "nv_expired", "message": f"The NV certificate expired on {valid_until}. "
                         "Banks withhold tax again until a new one is filed."})
    elif days is not None and days <= NV_EXPIRY_WARNING_DAYS:
        warnings.append({"code": "nv_expiring", "message": f"The NV certificate expires on {valid_until}. "
                         "Apply for a new one at the Finanzamt in time."})
    kind = settings.get("tax_health_insurance") or "unknown"
    if kind == "de_family":
        warnings.append({"code": "family_insurance", "message": "German family health insurance has an income "
                         "limit that capital income above the Sparer-Pauschbetrag counts toward. The room below "
                         "is capped at it."})
    elif kind == "foreign":
        warnings.append({"code": "foreign_insurance", "message": "Austrian ÖGK co-insurance of a child in "
                         "education has no income limit (Sec. 123 ASVG), but for a family member living in Germany "
                         "EU rules (Reg. 883/2004 Art. 1(i)) may test the German family-insurance limit instead, "
                         "so the room is kept within it to be safe. Austrian Familienbeihilfe (17,212 € a year, "
                         "Sec. 5 FLAG) does not count income taxed at the flat rate."})
    elif kind == "unknown":
        warnings.append({"code": "insurance_unknown", "message": "Set your health insurance in the tax settings: "
                         "German family insurance has an income limit that realised gains count toward."})
    if settings.get("tax_bafoeg"):
        warnings.append({"code": "bafoeg", "message": "BAföG counts capital income and assets over the "
                         "allowance; a realised gain can reduce it."})
    warnings.append({"code": "return_nv", "message": "Hand the NV certificate back to every bank once its "
                     "conditions stop holding, e.g. when you start working."})
    return warnings, {"valid_until": valid_until.isoformat() if valid_until else None, "expires_in_days": days}
