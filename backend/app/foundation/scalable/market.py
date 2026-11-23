"""Typed market reads through the read-only ``sc`` wrapper (ADR 0027).

Quotes, the derivatives search, price charts and security news for the
paper-only derivatives desk. Every call goes through ``ScalableCli.run``, so
the same allow-list applies; nothing here can place or change an order.

Money is parsed into ``Decimal`` and an unknown shape raises
``ScalableProtocolError``: a changed output format must stop the desk, not
trade on zeros.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .cli import ScalableCli, ScalableProtocolError, ScalableUsageError, validate_argv
from .mapping import as_dict, to_decimal


class WrapperOutdated(ScalableUsageError):
    """The installed wrapper refuses a desk read: ``install.sh`` has not been run again."""


@dataclass(frozen=True)
class Quote:
    isin: str
    name: str
    security_type: str
    bid: Decimal
    ask: Decimal
    mid: Decimal
    currency: str
    ts_utc: datetime | None
    outdated: bool


@dataclass(frozen=True)
class DerivativeListing:
    isin: str
    issuer: str
    strategy: str
    subcategory: str
    leverage: Decimal | None
    barrier: Decimal | None
    distance_to_knockout: Decimal | None
    strike: Decimal | None
    expiry: date | None
    delta: Decimal | None
    omega: Decimal | None
    implied_volatility: Decimal | None
    premium_percentage: Decimal | None
    underlying_isin: str


def _run(cli: ScalableCli, *args: str) -> dict[str, Any]:
    validate_argv(args)  # a bug on our side raises here, before the wrapper is involved
    try:
        return cli.run(*args)
    except ScalableUsageError as exc:
        if exc.code == "command_not_allowed":
            raise WrapperOutdated(
                "wrapper_outdated", "the sc wrapper does not know the desk reads yet",
                ["Run infra/scalable/install.sh as root again."],
            ) from exc
        raise


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_quote(result: dict[str, Any]) -> Quote | None:
    """``None`` when bid or ask is null (an index, or a product with no market)."""
    isin = result.get("isin")
    if not isinstance(isin, str):
        raise ScalableProtocolError("unexpected_shape", "sc quote output has no isin")
    bid = to_decimal(result.get("quote_bid_price"))
    ask = to_decimal(result.get("quote_ask_price"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = to_decimal(result.get("quote_mid_price")) or (bid + ask) / 2
    return Quote(
        isin=isin, name=str(result.get("name") or "")[:200],
        security_type=str(result.get("security_type") or ""),
        bid=bid, ask=ask, mid=mid, currency=str(result.get("quote_currency") or "EUR"),
        ts_utc=_parse_ts(result.get("quote_timestamp_utc")),
        outdated=bool(result.get("quote_is_outdated")),
    )


def get_quote(cli: ScalableCli, isin: str) -> Quote | None:
    return parse_quote(_run(cli, "broker", "quote", "--isin", isin))


def get_security_type(cli: ScalableCli, isin: str) -> str | None:
    """The instrument's security type from ``broker quote`` (``DERIVATIVE``, ``ETF`` ...), also when it
    has no bid or ask (a knocked-out product, the night): ``get_quote`` drops such a quote whole."""
    raw = _run(cli, "broker", "quote", "--isin", isin)
    result = as_dict(raw.get("result")) if isinstance(raw.get("result"), dict) else raw
    value = str(result.get("security_type") or "").strip().upper()
    return value or None


def _epoch_day(value: Any) -> date | None:
    raw = value.get("epoch_day") if isinstance(value, dict) else value
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        return date(1970, 1, 1) + timedelta(days=int(raw))
    except (ValueError, OverflowError):
        return None


def parse_listings(result: dict[str, Any]) -> list[DerivativeListing]:
    items = result.get("items")
    if not isinstance(items, list):
        raise ScalableProtocolError("unexpected_shape", "sc derivatives search output has no items")
    out: list[DerivativeListing] = []
    for item in items:
        row = as_dict(item)
        isin = row.get("isin")
        if not isinstance(isin, str):
            continue
        out.append(DerivativeListing(
            isin=isin, issuer=str(row.get("issuer") or ""), strategy=str(row.get("strategy") or ""),
            subcategory=str(row.get("product_subcategory") or ""),
            leverage=to_decimal(row.get("leverage")),
            barrier=to_decimal(as_dict(row.get("knockout_barrier")).get("value")),
            distance_to_knockout=to_decimal(row.get("distance_to_knockout")),
            strike=to_decimal(as_dict(row.get("strike")).get("value")),
            expiry=None if row.get("expiry_is_open_end") else _epoch_day(row.get("expiry_date")),
            delta=to_decimal(row.get("delta")), omega=to_decimal(row.get("omega")),
            implied_volatility=to_decimal(row.get("implied_volatility")),
            premium_percentage=to_decimal(row.get("premium_percentage")),
            underlying_isin=str(row.get("underlying_isin") or ""),
        ))
    return out


def search_derivatives(
    cli: ScalableCli, *, underlying: str, product_type: str, strategy: str, limit: int = 100,
    leverage_max: str | None = None, expiry_from: date | None = None, expiry_to: date | None = None,
    sort_field: str | None = None, sort_order: str | None = None, offset: int = 0,
) -> list[DerivativeListing]:
    args = ["broker", "derivatives", "search", "--underlying", underlying, "--type", product_type,
            "--strategy", strategy, "--limit", str(limit)]
    if offset:
        args += ["--offset", str(offset)]
    if leverage_max:
        args += ["--leverage-max", leverage_max]
    if expiry_from:
        args += ["--expiry-from", expiry_from.isoformat()]
    if expiry_to:
        args += ["--expiry-to", expiry_to.isoformat()]
    if sort_field:
        args += ["--sort-field", sort_field, "--sort-order", sort_order or "asc"]
    return parse_listings(_run(cli, *args))


def get_chart(cli: ScalableCli, isin: str, timeframe: str) -> list[tuple[datetime, Decimal]]:
    """``(timestamp_utc, mid)`` points, oldest first. ``1d`` is refused by sc, so it is not allowed."""
    data = _run(cli, "broker", "chart", "--isin", isin, "--timeframe", timeframe)
    points = data.get("data_points")
    if points is None:
        points = as_dict(data.get("result")).get("data_points")
    if not isinstance(points, list):
        raise ScalableProtocolError("unexpected_shape", "sc chart output has no data_points")
    out: list[tuple[datetime, Decimal]] = []
    for point in points:
        row = as_dict(point)
        ts, mid = _parse_ts(row.get("timestamp_utc")), to_decimal(row.get("mid_price"))
        if ts is not None and mid is not None:
            out.append((ts, mid))
    return sorted(out, key=lambda p: p[0])


def get_security_news(cli: ScalableCli, isin: str, locale: str = "de_DE") -> list[dict[str, Any]]:
    """Headlines as plain dicts. The output shape is unverified, so this parses defensively."""
    data = _run(cli, "broker", "security-news", "--isin", isin, "--locale", locale)
    candidates: Sequence[Any] = ()
    for source in (data, as_dict(data.get("result"))):
        for key in ("items", "news", "articles", "value"):
            if isinstance(source.get(key), list):
                candidates = source[key]
                break
        if candidates:
            break
    out: list[dict[str, Any]] = []
    for entry in candidates:
        row = as_dict(entry)
        title = row.get("title") or row.get("headline")
        if not isinstance(title, str) or not title.strip():
            continue
        out.append({
            "id": str(row.get("id") or row.get("url") or title)[:200],
            "title": title.strip()[:300],
            "published": str(row.get("published_at") or row.get("timestamp_utc") or row.get("date") or "")[:40],
        })
    return out
