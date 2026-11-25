"""What an issuer charges on a warrant over the listed option (ADR 0026).

Each evening the worker keeps one :class:`DerivativeQuote` per held warrant:
the volatility its price implies and Yahoo's implied volatility of the listed
option nearest in expiry and strike. The gap, and the share of the warrant's
price it accounts for, is the issuer's margin plus the bid-ask spread; studies
of German bank-issued warrants put it at up to 30 % of the price (Baule &
Blonski 2015; EUWAX data), more around events. Expiries differ (a warrant on
23 Dec against the nearest listed one), so the figure is an estimate.
"""
from __future__ import annotations

import statistics
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.derivatives import option_price


def _price_gap(row: dict[str, Any]) -> float | None:
    """Warrant price at its own implied vol over the price at the listed one, minus 1."""
    spot, strike, days = row.get("underlying_close"), row.get("strike"), row.get("days_to_expiry")
    if not spot or not strike or not days or days <= 0:
        return None
    years = days / 365.0
    rate, q = row.get("rate", 0.0), row.get("dividend_yield", 0.0)
    own = option_price(row["kind"], row["style"], spot, strike, years, rate, row["warrant_iv"], q)
    fair = option_price(row["kind"], row["style"], spot, strike, years, rate, row["listed_iv"], q)
    return own / fair - 1.0 if fair > 0 else None


def warrant_margin(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Median volatility gap and price premium over the listed options, overall and per warrant."""
    usable = [r for r in rows if r.get("warrant_iv") and r.get("listed_iv")]
    by_isin: dict[str, dict[str, Any]] = {}
    for r in usable:
        entry = by_isin.setdefault(r["isin"], {"days": 0, "vol_gaps": [], "price_gaps": []})
        entry["days"] += 1
        entry["vol_gaps"].append(r["warrant_iv"] - r["listed_iv"])
        gap = _price_gap(r)
        if gap is not None:
            entry["price_gaps"].append(gap)
    all_vol = [g for e in by_isin.values() for g in e["vol_gaps"]]
    all_price = [g for e in by_isin.values() for g in e["price_gaps"]]
    return {
        "quotes": len(usable),
        "warrants": len(by_isin),
        "median_vol_gap": statistics.median(all_vol) if all_vol else None,
        "median_price_premium": statistics.median(all_price) if all_price else None,
        "by_isin": {
            isin: {
                "days": e["days"],
                "median_vol_gap": statistics.median(e["vol_gaps"]),
                "median_price_premium": statistics.median(e["price_gaps"]) if e["price_gaps"] else None,
            }
            for isin, e in by_isin.items()
        },
        "estimate": True,
    }


def warrant_margin_from_db(db: Session) -> dict[str, Any]:
    """:func:`warrant_margin` over every stored quote that has a listed volatility."""
    from app.foundation.derivatives import pricing_inputs
    from app.foundation.models.entities import DerivativeQuote, DerivativeTerms

    terms = {t.isin: t for t in db.query(DerivativeTerms).all()}
    rows: list[dict[str, Any]] = []
    for q in db.query(DerivativeQuote).filter(DerivativeQuote.listed_iv.isnot(None)).all():
        t = terms.get(q.isin)
        if t is None:
            continue
        rate, div = pricing_inputs(db, t.underlying_symbol, t.strike_currency)
        rows.append({
            "isin": q.isin, "kind": t.kind, "style": t.style, "strike": float(t.strike),
            "underlying_close": q.underlying_close, "days_to_expiry": q.days_to_expiry,
            "warrant_iv": q.warrant_iv, "listed_iv": q.listed_iv, "rate": rate, "dividend_yield": div,
        })
    return warrant_margin(rows)
