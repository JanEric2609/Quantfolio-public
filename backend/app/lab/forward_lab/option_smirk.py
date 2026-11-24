"""The option-smirk signal (ADR 0028, research only; never a desk cell).

Xing, Zhang & Zhao (2010): a steep smirk, where out-of-the-money puts cost much more implied volatility than
at-the-money calls, predicts weak returns over the next weeks. Alpaca's option snapshots carry implied
volatility and Greeks but no open interest, so dealer gamma cannot be computed; the smirk can.

Score = -(IV of the OTM put with |delta| nearest 0.25 minus the IV of the call with delta nearest 0.5), on the
expiry 20-60 days out that is nearest 40 days. Higher = flatter smirk = predicted to do better. A name without
a usable chain is left out. The chain is read once a night just before the lab scores, throttled to 150
requests a minute (Basic allows 200), and kept in one internal ``app_settings`` key.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.providers.alpaca_provider import AlpacaProvider, DataUnavailable
from app.foundation.settings import get_setting_row, upsert_public_settings

logger = logging.getLogger(__name__)

KEY = "forward_lab_option_smirk_json"
MIN_DAYS, MAX_DAYS, TARGET_DAYS = 20, 60, 40
PUT_DELTA, CALL_DELTA = 0.25, 0.5
REQUESTS_PER_MINUTE = 150
#: Stop after this long: the night has other jobs.
DEADLINE_S = 45 * 60


class TokenBucket:
    """At most *rate* requests a minute; ``wait`` sleeps when the minute's tokens are used."""

    def __init__(self, rate: int = REQUESTS_PER_MINUTE, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate, self.clock, self.sleep = rate, clock, sleep
        self.tokens = float(rate)
        self.at = clock()

    def wait(self) -> None:
        now = self.clock()
        self.tokens = min(float(self.rate), self.tokens + (now - self.at) * self.rate / 60.0)
        self.at = now
        if self.tokens < 1.0:
            pause = (1.0 - self.tokens) * 60.0 / self.rate
            self.sleep(pause)
            self.at = self.clock()
            self.tokens = 1.0
        self.tokens -= 1.0


def smirk_score(chain: list[dict[str, Any]], today: date) -> float | None:
    """-(OTM put IV - ATM call IV) on the expiry nearest 40 days (20-60 days out), or ``None``."""
    by_expiry: dict[date, list[dict[str, Any]]] = {}
    for row in chain:
        days = (row["expiration"] - today).days
        if MIN_DAYS <= days <= MAX_DAYS:
            by_expiry.setdefault(row["expiration"], []).append(row)
    for expiry in sorted(by_expiry, key=lambda e: abs((e - today).days - TARGET_DAYS)):
        rows = by_expiry[expiry]
        puts = [r for r in rows if r["type"] == "put" and r["delta"] < 0]
        calls = [r for r in rows if r["type"] == "call" and r["delta"] > 0]
        if not puts or not calls:
            continue
        put = min(puts, key=lambda r: abs(abs(r["delta"]) - PUT_DELTA))
        call = min(calls, key=lambda r: abs(r["delta"] - CALL_DELTA))
        if abs(abs(put["delta"]) - PUT_DELTA) > 0.15 or abs(call["delta"] - CALL_DELTA) > 0.2:
            continue
        return -(put["iv"] - call["iv"])
    return None


def collect(db: Session, provider: AlpacaProvider, symbols: list[str], now: datetime,
            bucket: TokenBucket | None = None, deadline_s: float = DEADLINE_S,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read one chain per symbol (each page counts against the bucket) and store the scores.

    A 429 (rate limited despite the bucket) stops the night's scan like the deadline does: the names read so far
    are kept, and ``stopped`` says why, in the result and in the stored value."""
    bucket = bucket or TokenBucket()
    started = clock()
    today = now.astimezone(UTC).date()
    scores: dict[str, float] = {}
    stopped = None
    for sym in symbols:
        if clock() - started > deadline_s:
            stopped = "deadline"
            break
        try:
            chain = provider.option_chain(sym, today + timedelta(days=MIN_DAYS), today + timedelta(days=MAX_DAYS),
                                          before_request=bucket.wait)
        except DataUnavailable:
            stopped = "options_not_entitled"
            break
        except (ValueError, OSError) as exc:
            if "HTTP 429" in str(exc):
                stopped = "rate_limited"
                logger.info("option smirk: rate limited after %d name(s); stopping tonight's scan", len(scores))
                break
            logger.debug("option chain failed for a symbol: %s", type(exc).__name__)
            continue
        value = smirk_score(chain, today)
        if value is not None:
            scores[sym] = round(value, 6)
    upsert_public_settings(db, {KEY: {"collected_on": today.isoformat(), "scores": scores, "stopped": stopped}})
    db.commit()
    return {"names": len(scores), "asked": len(symbols), "stopped": stopped}


def stored_scores(db: Session, as_of: str) -> dict[str, float]:
    """The scores collected the night after *as_of* (never earlier: no look-ahead), else empty."""
    value = get_setting_row(db, KEY)
    if not isinstance(value, dict):
        return {}
    try:
        collected = date.fromisoformat(str(value.get("collected_on")))
        day = date.fromisoformat(as_of)
    except ValueError:
        return {}
    if not (day < collected <= day + timedelta(days=4)):
        return {}
    return {k: float(v) for k, v in (value.get("scores") or {}).items()}
