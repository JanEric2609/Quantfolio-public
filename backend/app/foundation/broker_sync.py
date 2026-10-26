"""Shared by every synced broker: the steps after a sync, and the ledger → tax cockpit import.

Scalable Capital and Trading 212 call these with their own source names; DKB
keeps its own path. Flat foundation module so neither broker package imports
the other.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.live_positions import asset_type_for
from app.foundation.models.entities import ActivityLedgerEntry, TaxLedgerEvent

logger = logging.getLogger(__name__)


def _book_snapshot(db: Session, user_id: str) -> int:
    """Today's positions at every broker, for the book's time-weighted return."""
    from app.foundation.portfolio_service import snapshot_book_positions

    return int(snapshot_book_positions(db, user_id).get("snapshots_created", 0))


def post_sync_steps(db: Session, user_id: str, *, tax_step: Callable[[], dict[str, Any]],
                    label: str) -> dict[str, Any]:
    """The steps after a broker sync committed, each on its own: one failing never undoes the sync.

    Ticker resolution, the holdings mirror, today's book snapshot, the broker's
    tax events (*tax_step*), fund classification and recommendation executions.
    """
    results: dict[str, Any] = {}
    steps: list[tuple[str, Callable[[], Any]]] = []

    def _resolve() -> Any:
        from app.foundation.portfolio.isin_resolver import resolve_isin_to_ticker
        return resolve_isin_to_ticker(db, user_id).get("resolved", 0)

    def _mirror() -> Any:
        from app.foundation.portfolio_service import sync_dkb_to_wealth_ledger
        return sync_dkb_to_wealth_ledger(db, user_id)

    def _classify() -> Any:
        from app.foundation.etf_classification import classify_and_enrich
        return classify_and_enrich(db, user_id).get("classified", "done")

    steps.append(("tickers_resolved", _resolve))
    steps.append(("mirror", _mirror))
    steps.append(("book_snapshot", lambda: _book_snapshot(db, user_id)))
    steps.append(("tax", tax_step))
    steps.append(("classified", _classify))

    def _executions() -> Any:
        from app.foundation.recommendation_execution import detect_executions
        return len(detect_executions(db, user_id))

    steps.append(("executions_detected", _executions))
    for name, fn in steps:
        try:
            value = fn()
            if name == "tax":
                results["tax_warnings"] = value.pop("tax_warnings", [])
            results[name] = value
        except Exception:
            db.rollback()
            logger.warning("%s post-step %s failed", label, name, exc_info=True)
            results[name] = "failed"
    return {"post_steps": results}


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except ArithmeticError:
        return None


def _ledger_raw(row: ActivityLedgerEntry) -> dict[str, Any]:
    try:
        raw = json.loads(row.raw_json or "{}")
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def ingest_broker_tax_events(db: Session, user_id: str, *, source: str, lot_source: str, label: str,
                             security_types: dict[str, Any]) -> dict[str, Any]:
    """A broker's wealth ledger into the tax cockpit: lots, sales, dividends, interest, withheld tax.

    * Every executed buy becomes a FIFO lot booked to the broker's depot
      (``account_ref``). FIFO runs per depot (§ 20 Abs. 4 S. 7 EStG), so the
      lots of the same ISIN at another bank are never consumed by this sale.
    * Every executed sell consumes those lots FIFO and becomes a ``sale``
      event; a share sale goes to the Aktien loss pot, a fund sale to the
      general one. A sale with no lots to consume is named, never guessed.
    * Dividends and interest become events at the gross the ledger row
      carries (``raw["gross"]``), or at the booked credit when there is none.
    * Tax the broker withheld or refunded (``raw["tax"]``) becomes a
      ``withholding`` event.

    *security_types* maps an ISIN to the broker's security type (ETF, STOCK, …).
    A row in another currency is converted at the ECB reference rate of its
    own day (§ 20 Abs. 4 S. 1 Hs. 2 EStG: each leg at its own rate;
    ``foundation/ecb_fx.py``, market rate as the fallback); a row with no rate
    at all is named in ``tax_warnings``, never booked at par. Deduped on
    ``source_ref = "<source>:<external id>"``; one row failing never blocks the
    others. Estimates: the broker's statements are the source of truth.
    """
    from app.foundation.ecb_fx import eur_per_unit
    from app.foundation.models.entities import TaxLot
    from app.foundation.tax_calc import classify_loss_bucket, teilfreistellung_pct_for_fund_class
    from app.foundation.tax_cockpit import _classify_holding, _safe_etf_index, create_event, record_sale_via_fifo

    rows = (
        db.query(ActivityLedgerEntry)
        .filter(
            ActivityLedgerEntry.user_id == user_id,
            ActivityLedgerEntry.source == source,
            ActivityLedgerEntry.activity_type.in_(("buy", "sell", "dividend", "interest")),
        )
        .order_by(ActivityLedgerEntry.date, ActivityLedgerEntry.created_at)
        .all()
    )
    result: dict[str, Any] = {
        "lots": 0, "sales": 0, "events": 0, "withholding": 0, "converted": 0, "tax_warnings": [],
    }
    if not rows:
        return result

    fx_cache: dict[tuple[str, Any], tuple[Decimal | None, str]] = {}
    warned: set[str] = set()

    def fx(row: ActivityLedgerEntry) -> Decimal | None:
        """EUR per unit of the row's currency on the row's day (1 for EUR); warns once per row when missing."""
        ccy = (row.currency or "EUR").upper()
        if ccy == "EUR":
            return Decimal(1)
        key = (ccy, row.date)
        if key not in fx_cache:
            rate, source = eur_per_unit(db, ccy, row.date)
            fx_cache[key] = (Decimal(str(rate)) if rate else None, source)
        rate, _source = fx_cache[key]
        if rate is None and row.id not in warned:
            warned.add(row.id)
            result["tax_warnings"].append(
                f"no {ccy}/EUR rate for {row.date.isoformat()}; the {row.activity_type} of "
                f"{row.isin or row.symbol or 'a security'} that day is not in the tax estimate"
            )
        return rate

    def eur(value: Any, rate: Decimal) -> Decimal | None:
        amount = _decimal_or_none(value)
        return None if amount is None else amount * rate
    etf_index: dict[str, Any] | None = None

    def tax_class(isin: str) -> tuple[str, str]:
        """(fund class, loss bucket) for *isin*, from the broker's security type and fund evidence."""
        nonlocal etf_index
        asset = asset_type_for(security_types.get(isin))
        if asset in {"stock", "bond", "etc"}:
            # Not an investment fund: no Teilfreistellung.
            return "other", classify_loss_bucket(asset, None)
        if etf_index is None:
            etf_index = _safe_etf_index()
        fund_class, _source = _classify_holding(user_fund_class=None, isin=isin, etf_index=etf_index)
        return fund_class, classify_loss_bucket("fund", fund_class)

    lot_refs = {
        ref for (ref,) in db.query(TaxLot.source_ref).filter(TaxLot.user_id == user_id, TaxLot.source_ref.is_not(None))
    }
    event_refs = {
        ref for (ref,) in db.query(TaxLedgerEvent.source_ref).filter(
            TaxLedgerEvent.user_id == user_id, TaxLedgerEvent.source_ref.like(f"{source}:%"),
        )
    }

    # 1. Lots from buys (before any sale, so a sale can consume them).
    for row in rows:
        if row.activity_type != "buy" or not row.isin:
            continue
        ref = f"activity:{row.id}"
        qty = Decimal(str(row.quantity or 0))
        if ref in lot_refs or qty <= 0:
            continue
        rate = fx(row)
        if rate is None:
            continue
        fees = Decimal(str(row.fees or 0)) * rate
        # Acquisition cost includes the order fee (Anschaffungsnebenkosten).
        # The booked amount is what left the account; when it matches price x
        # quantity without the fee, the fee is added.
        booked = abs(Decimal(str(row.amount or 0))) * rate
        implied = qty * Decimal(str(row.price or 0)) * rate
        if not booked:
            cost = implied + fees
        elif fees and abs(booked - implied) < fees / 2:
            cost = booked + fees
        else:
            cost = booked
        fund_class, _bucket = tax_class(row.isin)
        db.add(TaxLot(
            user_id=user_id, isin=row.isin, symbol=row.symbol,
            name=(row.description or "")[:200] or None, account_ref=row.connected_account_id,
            fund_class=fund_class, teilfreistellung_pct=teilfreistellung_pct_for_fund_class(fund_class),
            acquired_at=row.date, quantity_initial=qty, quantity_remaining=qty,
            cost_basis_eur=cost, fees_eur=fees, source=lot_source, source_ref=ref,
        ))
        lot_refs.add(ref)
        result["lots"] += 1
        result["converted"] += int(rate != 1)
    db.commit()

    def withholding(row: ActivityLedgerEntry, tax: Decimal | None, isin: str | None) -> None:
        ref = f"{source}:{row.external_id}:tax"[:120]
        if tax is None or tax == 0 or ref in event_refs:
            return
        try:
            create_event(db, user_id, {
                "event_type": "withholding", "event_date": row.date, "tax_year": row.date.year,
                "isin": isin, "withheld_eur": tax, "confidence": "estimate",
                "source": lot_source, "source_ref": ref,
                "notes": f"Tax {label} withheld (negative: refunded) on this transaction; "
                         "the broker's statement remains the source of truth.",
            })
            event_refs.add(ref)
            result["withholding"] += 1
        except Exception:
            db.rollback()
            logger.warning("%s withholding event failed", label, exc_info=True)

    for row in rows:
        if not row.external_id or row.activity_type == "buy":
            continue
        rate = fx(row)
        if rate is None:
            continue
        ref = f"{source}:{row.external_id}"[:120]
        raw = _ledger_raw(row)
        tax = eur(raw.get("tax"), rate)
        if row.activity_type == "sell" and row.isin:
            if ref in event_refs:
                withholding(row, tax, row.isin)
                continue
            qty = abs(Decimal(str(row.quantity or 0)))
            price = eur(row.price, rate)
            if qty <= 0 or price is None:
                result["tax_warnings"].append(
                    f"the sale of {row.isin} on {row.date.isoformat()} has no quantity or price; its gain is not estimated"
                )
                # The tax the broker withheld was paid whether or not the gain
                # can be estimated.
                withholding(row, tax, row.isin)
                continue
            fund_class, bucket = tax_class(row.isin)
            try:
                record_sale_via_fifo(
                    db, user_id, isin=row.isin, sell_date=row.date, sell_quantity=qty,
                    sell_price_per_unit_eur=price, sell_fees_eur=Decimal(str(row.fees or 0)) * rate,
                    fund_class=fund_class, bucket=bucket, source=lot_source, source_ref=ref,
                    account_ref=row.connected_account_id, acquired_on_or_before=row.date,
                )
            except ValueError:
                db.rollback()
                result["tax_warnings"].append(
                    f"no tax lots at {label} cover the sale of {row.isin} on {row.date.isoformat()}; "
                    "add its purchase in the tax cockpit to estimate the gain"
                )
                withholding(row, tax, row.isin)
                continue
            event_refs.add(ref)
            result["sales"] += 1
            result["converted"] += int(rate != 1)
            withholding(row, tax, row.isin)
        elif row.activity_type in {"dividend", "interest"}:
            if ref not in event_refs and row.amount and Decimal(row.amount) > 0:
                gross = eur(raw.get("gross"), rate) or Decimal(row.amount) * rate
                event: dict[str, Any] = {
                    "event_type": row.activity_type,
                    "event_date": row.date,
                    "tax_year": row.date.year,
                    "gross_eur": gross,
                    # Set only when the broker reported the split, so the allowance
                    # planner still treats a bare credit as possibly net.
                    "withheld_eur": tax if tax is not None else Decimal("0"),
                    "foreign_wht_eur": Decimal("0"),
                    "confidence": "estimate",
                    "source": lot_source,
                    "source_ref": ref,
                    "notes": f"Booked by {label} (gross from the broker's details when given); the "
                             "broker's statement (Ertragsabrechnung) remains the source of truth.",
                }
                if row.activity_type == "dividend" and row.isin:
                    fund_class, _bucket = tax_class(row.isin)
                    event["isin"] = row.isin
                    event["fund_class"] = fund_class
                try:
                    create_event(db, user_id, event)
                    event_refs.add(ref)
                    result["events"] += 1
                except Exception:
                    db.rollback()
                    logger.warning("%s tax event for a %s row failed", label, row.activity_type, exc_info=True)
                    continue
            withholding(row, tax, row.isin)
    return result
