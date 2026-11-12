"""The learning sleeve's ledger: what was planned, what the owner bought, what the shadow ETF did.

ADR 0025 amendment 2026-11-12. The owner places every order; this module only
records the plan line, detects the filled position in the synced Trading 212
holdings, and compares the purchase with the same amount in the benchmark ETF.
Results are a learning expense, never evidence about the picks, and are read
only by the learning-sleeve endpoint.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.live_positions import live_positions
from app.foundation.market import latest_cached_close
from app.foundation.models.entities import LearningPurchase
from app.foundation.settings import get_public_settings

BROKER = "trading212"
WATCH_DAYS = 45
NOTE = "A learning expense you chose. It is not evidence that the picks work."


def _quantity(db: Session, user_id: str, isin: str) -> float:
    total = Decimal(0)
    for pos in live_positions(db, user_id):
        if pos.source == BROKER and pos.isin and pos.isin.upper() == isin.upper():
            total += Decimal(pos.quantity or 0)
    return float(total)


def record_planned(db: Session, user_id: str, line: dict[str, Any], month: date) -> LearningPurchase | None:
    """Store this month's learning line once (the first plan of the month wins). The caller commits."""
    pick = line.get("pick")
    amount = float(line.get("amount_eur") or 0.0)
    if not pick or amount <= 0 or not pick.get("isin"):
        return None
    month = month.replace(day=1)
    row = db.query(LearningPurchase).filter_by(user_id=user_id, month=month).first()
    if row is not None:
        return row
    shadow = str(get_public_settings(db).get("passive_core_ticker") or "EUNL.DE")
    row = LearningPurchase(
        user_id=user_id, month=month, isin=str(pick["isin"]).upper(), ticker=str(pick.get("ticker") or ""),
        name=pick.get("name"), amount_eur=amount, paper_price=latest_cached_close(db, str(pick.get("ticker") or "")),
        shadow_ticker=shadow, shadow_price=latest_cached_close(db, shadow),
        baseline_quantity=_quantity(db, user_id, str(pick["isin"])),
    )
    db.add(row)
    return row


def detect_fills(db: Session, user_id: str, *, now: datetime | None = None) -> list[str]:
    """Mark planned months whose pick grew in the synced Trading 212 position; returns the ids changed."""
    now = now or datetime.now(UTC)
    changed: list[str] = []
    for row in db.query(LearningPurchase).filter_by(user_id=user_id, status="planned").all():
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
        if now - created > timedelta(days=WATCH_DAYS):
            row.status = "skipped"
            changed.append(row.id)
            continue
        delta = _quantity(db, user_id, row.isin) - float(row.baseline_quantity or 0.0)
        if delta > 0:
            row.status, row.fill_quantity, row.filled_at = "filled", delta, now
            changed.append(row.id)
    if changed:
        db.commit()
    return changed


def record_fill_price(db: Session, user_id: str, purchase_id: str, price_eur: float) -> LearningPurchase | None:
    row = db.query(LearningPurchase).filter_by(id=purchase_id, user_id=user_id).first()
    if row is None:
        return None
    row.fill_price_eur = float(price_eur)
    db.commit()
    return row


def results(db: Session, user_id: str) -> dict[str, Any]:
    """Both results, the count and the real fill against the paper price."""
    rows = db.query(LearningPurchase).filter_by(user_id=user_id).order_by(LearningPurchase.month).all()
    out_rows: list[dict[str, Any]] = []
    real_in = real_now = shadow_now = 0.0
    filled = 0
    last_cache: dict[str, float | None] = {}

    def last(ticker: str) -> float | None:
        if ticker not in last_cache:
            last_cache[ticker] = latest_cached_close(db, ticker)
        return last_cache[ticker]

    for r in rows:
        item: dict[str, Any] = {
            "id": r.id, "month": r.month.isoformat(), "ticker": r.ticker, "name": r.name, "isin": r.isin,
            "amount_eur": r.amount_eur, "status": r.status, "paper_price": r.paper_price,
            "fill_quantity": r.fill_quantity, "fill_price_eur": r.fill_price_eur, "fill_price_is_implied": False,
        }
        if r.status == "filled" and r.fill_quantity:
            price = r.fill_price_eur
            if price is None:
                price = r.amount_eur / r.fill_quantity
                item["fill_price_eur"], item["fill_price_is_implied"] = price, True
            if r.paper_price:
                item["fill_vs_paper_pct"] = round((price / r.paper_price - 1.0) * 100.0, 2)
            now_px, sh_now = last(r.ticker), last(r.shadow_ticker)
            if now_px:
                item["value_now_eur"] = round(r.fill_quantity * now_px, 2)
                real_now += r.fill_quantity * now_px
            if r.shadow_price and sh_now:
                item["shadow_value_now_eur"] = round(r.amount_eur / r.shadow_price * sh_now, 2)
                shadow_now += r.amount_eur / r.shadow_price * sh_now
            real_in += r.amount_eur
            filled += 1
        out_rows.append(item)
    return {
        "purchases": out_rows, "count_filled": filled, "count_planned": len(rows),
        "invested_eur": round(real_in, 2), "real_value_eur": round(real_now, 2),
        "shadow_value_eur": round(shadow_now, 2), "note": NOTE,
        "estimate": True, "not_tax_advice": True, "not_financial_advice": True,
    }
