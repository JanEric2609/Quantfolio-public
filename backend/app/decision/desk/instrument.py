"""Exactly which instrument, in the words the owner types into the broker app (ADR 0027 amendment 2026-11-20).

Every desk message names the product by its issuer's name, ISIN and WKN, with
the terms that decide its risk (leverage and barrier, or omega and strike, and
the expiry), and the order line as it would be entered. A CFD names the
Trading 212 market and the quantity instead.
"""
from __future__ import annotations

from decimal import Decimal

from app.foundation.models.entities import DeskPosition


def _num(value: Decimal | float | None, places: int = 2) -> str:
    if value is None:
        return "?"
    return f"{Decimal(value):,.{places}f}"


def terms_line(p: DeskPosition) -> str:
    lev = _num(p.leverage_or_omega, 1)
    expiry = p.expiry.isoformat() if p.expiry else "open end"
    if p.product_type == "knockout":
        return f"Leverage {lev}, knock-out barrier {_num(p.barrier_or_strike)}, expiry {expiry}"
    if p.product_type == "warrant":
        return f"Omega {lev}, strike {_num(p.barrier_or_strike)}, expiry {expiry}"
    return ""


def identity_lines(p: DeskPosition) -> list[str]:
    """Name, ISIN/WKN and terms of a Scalable product (knock-out or warrant)."""
    kind = "Knock-out" if p.product_type == "knockout" else "Warrant"
    side = ("call" if p.direction == "long" else "put") if p.product_type == "warrant" else (
        "long" if p.direction == "long" else "short")
    lines = [f"{kind} {side} on {p.underlying_label}: {p.product_name or '(name not quoted)'}"]
    ident = f"ISIN {p.isin}" + (f", WKN {p.wkn}" if p.wkn else "") + (f", issuer {p.issuer}" if p.issuer else "")
    lines.append(ident)
    if terms := terms_line(p):
        lines.append(terms)
    return lines


def order_line(p: DeskPosition, side: str = "Buy", price: Decimal | None = None) -> str:
    """The order as it would be typed in the Scalable app (paper: nothing is sent)."""
    key = p.wkn or p.isin
    px = p.entry_price if price is None else price
    return f"{side} {p.units or 0} x {key} at limit {_num(px, 4)} EUR (gettex)"


def describe(p: DeskPosition) -> str:
    return "\n".join([*identity_lines(p), order_line(p)])
