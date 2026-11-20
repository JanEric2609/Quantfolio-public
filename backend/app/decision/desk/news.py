"""The news guard (ADR 0027): blocks and logs, never opens or closes a position.

Earnings dates come from the existing Finnhub calendar (US listings only); a
non-US underlying's headlines come from ``sc broker security-news``, a US one's
from Alpaca's news endpoint.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.foundation.providers.alpaca_provider import AlpacaProvider
from app.foundation.scalable import market
from app.foundation.scalable.cli import ScalableCli, ScalableError
from app.foundation.settings import get_secret

from .underlyings import Underlying

logger = logging.getLogger(__name__)


def alpaca_for(db: Session) -> AlpacaProvider | None:
    secret = get_secret(db, "alpaca")
    if not secret:
        return None
    key, meta = secret
    provider = AlpacaProvider(key, api_secret=(meta or {}).get("api_secret"))
    return provider if provider.enabled else None


def earnings_in_window(db: Session, symbol: str | None, start: date, end: date) -> bool | None:
    """True when a known earnings date falls in [start, end], False when the calendar answers with a
    date outside it or none, ``None`` when it cannot be read (no ticker, no key, a failed lookup).
    The caller decides what unknown means: for a US stock it is not "no earnings"."""
    if not symbol:
        return None
    try:
        from app.foundation.providers.registry import build_provider_registry

        result = build_provider_registry(db).get_earnings_calendar(symbol)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        logger.info("desk earnings lookup failed: %s", type(exc).__name__)
        return None
    if not isinstance(result, dict) or not result.get("ok", True):
        return None
    data = result.get("data")
    nxt = data.get("next_earnings_date") if isinstance(data, dict) else None
    if not nxt:
        return False
    try:
        return start <= date.fromisoformat(str(nxt)) <= end
    except ValueError:
        return None


def headlines(db: Session, cli: ScalableCli, underlying: Underlying, since: datetime,
              symbol: str | None = None) -> list[dict[str, Any]]:
    """Recent headlines as ``{"id", "title"}``; failures give an empty list (news is only a guard)."""
    try:
        sym = underlying.alpaca_symbol or (symbol if underlying.exchange == "XNYS" else None)
        if sym:
            provider = alpaca_for(db)
            if provider is None:
                return []
            return [{"id": n["id"], "title": n["headline"]} for n in provider.get_news([sym], since)]
        isin = underlying.proxy_isin or underlying.isin
        return [{"id": n["id"], "title": n["title"]} for n in market.get_security_news(cli, isin)
                if not _older_than(n.get("published"), since)]
    except market.WrapperOutdated:
        raise  # not a news problem: the monitor reports it
    except (ScalableError, ValueError, httpx.HTTPError) as exc:  # httpx: Alpaca timeouts and refused connections
        logger.info("desk news fetch failed: %s", type(exc).__name__)
        return []


def _older_than(published: str | None, since: datetime) -> bool:
    """True when sc's ``published`` stamp parses and lies before *since*; an unknown stamp is kept."""
    if not published:
        return False
    try:
        value = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (value if value.tzinfo else value.replace(tzinfo=UTC)) < since


def window_end(now: datetime, days: int = 2) -> date:
    return (now.astimezone(UTC) + timedelta(days=days)).date()
