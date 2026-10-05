import json
import logging
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Fundamental, MacroIndicator, PriceCache
from app.foundation.data_backbone.bars import BarStore
from app.foundation.providers.registry import build_provider_registry
from app.foundation.settings import get_public_settings


logger = logging.getLogger(__name__)

QUOTE_TTL = timedelta(minutes=15)
HISTORY_TTL = timedelta(hours=24)
FUNDAMENTALS_TTL = timedelta(days=7)
MACRO_TTL = timedelta(hours=24)

# Bar-history vintage tolerance (audit F16/F21): how old the NEWEST bar may be
# before the series is flagged stale. Public setting "history_stale_days",
# clamped at the read site.
_HISTORY_STALE_DAYS_DEFAULT = 7
_STALE_DAYS_MIN = 1
_STALE_DAYS_MAX = 30


def _history_stale_days(db: Session) -> int:
    """Clamped ``history_stale_days`` public setting (calendar days)."""
    raw = get_public_settings(db).get("history_stale_days", _HISTORY_STALE_DAYS_DEFAULT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _HISTORY_STALE_DAYS_DEFAULT
    return max(_STALE_DAYS_MIN, min(_STALE_DAYS_MAX, value))


def _to_utc(ts: Any) -> Any:
    """Normalise a naive timestamp to UTC (SQLite drops tzinfo on storage)."""
    if getattr(ts, "tzinfo", None) is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _is_fresh(fetched_at: datetime, ttl: timedelta) -> bool:
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return fetched_at >= datetime.now(UTC) - ttl


def quote(db: Session, ticker: str) -> dict[str, Any]:
    ticker = ticker.upper()
    cached = (
        db.query(PriceCache)
        .filter(PriceCache.ticker == ticker)
        .order_by(PriceCache.date.desc(), PriceCache.fetched_at.desc())
        .first()
    )
    if cached and _is_fresh(cached.fetched_at, QUOTE_TTL):
        return {
            "ticker": ticker,
            "price": float(cached.close),
            "currency": cached.currency,
            "source": cached.source,
            "stale": False,
        }

    live_result = build_provider_registry(db).get_quote(ticker)
    live = live_result.get("data") if live_result.get("ok") else None
    if live and _is_valid_close(live.get("close")):
        _upsert_price(db, ticker, live)
        return {
            "ticker": ticker,
            "price": float(live["close"]),
            "currency": live.get("currency", "EUR"),
            "source": live.get("source", live_result.get("provider", "provider")),
            "stale": False,
            "message": "; ".join(live_result.get("quality", {}).get("warnings", [])) or None,
        }
    if cached:
        return {
            "ticker": ticker,
            "price": float(cached.close),
            "currency": cached.currency,
            "source": cached.source,
            "stale": True,
            "message": "Returning cached quote because live market data is unavailable.",
        }
    return {
        "ticker": ticker,
        "price": None,
        "currency": "EUR",
        "source": "unavailable",
        "stale": True,
        "message": "; ".join(live_result.get("quality", {}).get("warnings", [])) or "No quote is cached and live market data is unavailable.",
    }


def dividend_history(db: Session, ticker: str, start: date) -> list[dict[str, Any]]:
    """Cash distributions per share, in the listing's currency, with ex-date on or after ``start``.

    ``[{"ex_date": "YYYY-MM-DD", "amount": float}]``; empty when no provider
    knows (an accumulating fund has none).
    """
    result = build_provider_registry(db).get_dividends(ticker.upper(), start.isoformat())
    return list(result.get("data") or []) if result.get("ok") else []


def history(
    db: Session, ticker: str, days: int = 365, allow_live: bool = True
) -> list[dict[str, Any]]:
    """Return cached/live OHLCV history for ``ticker``.

    When ``allow_live`` is ``False`` the synchronous provider fallback is
    skipped entirely: only cached bars/prices are returned (possibly empty or
    stale). Callers that run in a request path over many symbols should pass
    ``allow_live=False`` to avoid a per-symbol live fetch that can hang.
    """
    ticker = ticker.upper()
    cutoff = date.today() - timedelta(days=days)

    # Check bar_prices hypertable first (highest-fidelity source).
    bar_store = BarStore(db)
    bars_df = bar_store.get_bars(
        symbol=ticker,
        start=datetime.combine(cutoff, datetime.min.time()),
    )
    if bars_df is not None and not bars_df.empty:
        # Vintage check (audit F16/F21): stale iff the NEWEST bar is older than
        # history_stale_days. All rows in one call share the flag. This is a
        # data-vintage concept — deliberately NOT the PriceCache fetch-recency
        # TTL (_is_fresh/HISTORY_TTL) used below; do not "align" them.
        newest_ts = _to_utc(max(bars_df["ts"]))
        is_stale = datetime.now(UTC) - newest_ts > timedelta(days=_history_stale_days(db))
        return [
            {
                "date": getattr(row, "ts").date() if hasattr(getattr(row, "ts"), "date") else getattr(row, "ts"),
                "open": float(getattr(row, "open")) if getattr(row, "open") is not None else None,
                "high": float(getattr(row, "high")) if getattr(row, "high") is not None else None,
                "low": float(getattr(row, "low")) if getattr(row, "low") is not None else None,
                "close": float(getattr(row, "close")),
                "volume": float(getattr(row, "volume")) if getattr(row, "volume") is not None else None,
                "source": getattr(row, "provider") or "bar_prices",
                "stale": is_stale,
            }
            for row in bars_df.itertuples(index=False)
        ]

    # Fetch-recency TTL for cached quotes — measures when we last FETCHED, not
    # how old the price vintage is (the bar branch above owns vintage).
    cached = (
        db.query(PriceCache)
        .filter(PriceCache.ticker == ticker, PriceCache.date >= cutoff)
        .order_by(PriceCache.date.asc())
        .all()
    )
    if cached and _is_fresh(max(row.fetched_at for row in cached), HISTORY_TTL):
        return [_price_to_dict(row, stale=False) for row in cached]

    if not allow_live:
        # Cache-only path: never trigger a synchronous provider fetch.
        return [_price_to_dict(row, stale=True) for row in cached]

    live_result = build_provider_registry(db).get_history(ticker, days=days)
    live = live_result.get("data") if live_result.get("ok") else None
    if live:
        reported = next((i.get("currency") for i in reversed(live) if i.get("currency")), None)
        if reported:
            from app.foundation.data_backbone.listing_currency import record_listing_currency

            record_listing_currency(db, ticker, reported, f"{live[-1].get('source') or 'provider'}_metadata")
        _upsert_prices(db, ticker, live, commit=False)
        db.commit()
        refreshed = (
            db.query(PriceCache)
            .filter(PriceCache.ticker == ticker, PriceCache.date >= cutoff)
            .order_by(PriceCache.date.asc())
            .all()
        )
        return [_price_to_dict(row, stale=False) for row in refreshed]
    return [_price_to_dict(row, stale=True) for row in cached]


# Per-process memo of catch-up attempts, so a symbol the providers no longer
# serve (delisted, renamed) costs one live call per window, not one per read.
_BAR_REFRESH_RETRY = timedelta(hours=6)
_bar_refresh_attempts: dict[str, datetime] = {}


def refresh_stale_bars(db: Session, ticker: str, max_age_days: float = 3.0) -> bool:
    """Catch a ``bar_prices`` series up when its newest bar is too old.

    :func:`history` serves stored bars without a live fetch whenever any
    exist (audit M2 kept provider calls out of that read path). A series
    that nothing re-ingests therefore freezes. On prod ``USDEUR=X`` stopped
    at 2026-09-01, so Discover restated three weeks of benchmark returns at
    one flat rate, and outcome resolution would have kept USD returns
    unconverted. Nightly jobs that need a current reference series call
    this before reading it.

    Returns True when new bars were written. A symbol with no stored bars
    is left to :func:`history`'s own live fallback.
    """
    ticker = ticker.upper()
    latest = BarStore(db).get_latest_bar(ticker)
    if latest is None or latest.get("ts") is None:
        return False
    raw_ts = latest["ts"]
    if isinstance(raw_ts, str):  # SQLite returns raw-SQL timestamps as text
        raw_ts = datetime.fromisoformat(raw_ts)
    newest = _to_utc(raw_ts)
    now = datetime.now(UTC)
    if now - newest <= timedelta(days=max_age_days):
        return False
    last_try = _bar_refresh_attempts.get(ticker)
    if last_try is not None and now - last_try < _BAR_REFRESH_RETRY:
        return False
    _bar_refresh_attempts[ticker] = now

    from app.foundation.data_backbone.ingest import DataIngester

    start = (newest.date() - timedelta(days=1)).isoformat()
    try:
        result = DataIngester(db).ingest_bar_prices(ticker, start_date=start)
    except Exception as exc:
        logger.warning("refresh_stale_bars: %s catch-up from %s failed: %s", ticker, start, exc)
        return False
    if not result.get("success"):
        logger.info("refresh_stale_bars: %s still stale since %s (%s)", ticker, newest.date(), result.get("message"))
        return False
    return True


def usd_per_unit_by_date(
    db: Session, currency: str, days: int = 365 * 3 + 30, *, allow_live: bool = True,
) -> dict[str, float] | None:
    """``{YYYY-MM-DD: USD per 1 unit of currency}`` from the ``USD<ccy>=X`` bars.

    The same pair Discover already ingests for its benchmark restatement
    (inverted here), caught up first so a frozen series is not forward-filled.
    Request paths pass ``allow_live=False`` (see :func:`history`). ``None`` for
    USD itself or when no rate is available.
    """
    ccy = currency.upper()
    if ccy == "USD":
        return None
    symbol = f"USD{ccy}=X"
    try:
        refresh_stale_bars(db, symbol)
        rows = history(db, symbol, days=days, allow_live=allow_live)
    except Exception as exc:
        logger.warning("usd_per_unit_by_date: %s unavailable: %s", symbol, exc)
        return None
    rates: dict[str, float] = {}
    for row in rows or []:
        close = row.get("close")
        try:
            value = float(close) if close is not None else 0.0
        except (TypeError, ValueError):
            continue
        if value > 0:
            rates[str(row.get("date"))[:10]] = 1.0 / value
    return rates or None


def latest_cached_close(db: Session, symbol: str) -> float | None:
    """Return the most recent cached close for *symbol*, or None.

    Uses cached bars only (``allow_live=False``) — callers in write/loop
    paths (discovery prediction ledger, advisor recommendations) must not
    trigger a per-symbol live fetch.
    """
    try:
        rows = history(db, symbol, days=30, allow_live=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("price lookup failed for %s: %s", symbol, exc)
        return None
    for row in reversed(rows):
        close = row.get("close")
        if close is not None:
            return float(close)
    return None


def ohlcv_frame(db: Session, tickers: list[str], start_date: str, end_date: str) -> Any:
    """Build a FinRL-shaped OHLCV DataFrame (date/open/high/low/close/volume/tic)
    via ``history()`` — provider-chain fallback + caching — instead of
    finrl's own ``YahooDownloader``.

    ``YahooDownloader.fetch_data()`` is unconditionally broken against this
    app's yfinance version on two independent fronts: it calls
    ``yf.download(..., proxy=proxy)``, a kwarg yfinance 1.4.1 removed
    entirely, AND even past that, expects an 8-flat-column return shape
    that modern yfinance no longer produces (it now returns a MultiIndex
    frame). finrl is dormant upstream — patching around one yfinance
    version drift just to hit the next isn't a bounded fix.

    Lives here, not in ``quant_rl`` (its only consumer): ``quant_rl`` is a
    foundation-shaped package the decision loop imports (Track D2's
    ``advisor.rl_training``), and an ``app.foundation.market`` edge from
    inside ``quant_rl`` would merge it into the decision loop's existing
    import-cycle SCC the moment ``advisor`` started consuming it —
    confirmed via ``check_no_new_cycles.py``. Callers fetch here and pass
    the result into ``quant_rl.envs.build_env``.

    Output columns match ``YahooDownloader``'s own documented shape so
    FinRL's own ``FeatureEngineer.preprocess_data``/``data_split`` work
    identically on the result.
    """
    import pandas as pd

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    # history()'s `days` is "how far back from TODAY", not the span between
    # start/end — a training window years in the past still needs `days` to
    # reach all the way back to `start`.
    days = max((date.today() - start).days, 1) + 5  # small buffer

    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        bars = history(db, ticker, days=days, allow_live=True)
        for bar in bars:
            bar_date = bar["date"]
            if isinstance(bar_date, str):
                bar_date = date.fromisoformat(bar_date)
            if not (start <= bar_date < end):
                continue
            rows.append({
                "date": bar_date.strftime("%Y-%m-%d"),
                "open": bar.get("open") if bar.get("open") is not None else bar["close"],
                "high": bar.get("high") if bar.get("high") is not None else bar["close"],
                "low": bar.get("low") if bar.get("low") is not None else bar["close"],
                "close": bar["close"],
                "volume": bar.get("volume") or 0.0,
                "tic": ticker,
            })

    if not rows:
        raise ValueError(
            f"No price history available for {tickers} in [{start_date}, {end_date})"
        )
    return pd.DataFrame(rows)


def fundamentals(db: Session, ticker: str) -> dict[str, Any]:
    ticker = ticker.upper()
    cached = db.query(Fundamental).filter(Fundamental.ticker == ticker).one_or_none()
    # Universe screening (discover/universe.py:_persist_fundamentals_batch)
    # writes a shallow {market_cap, volume, name}-only payload tagged with
    # this source to avoid a per-symbol live fetch during the ~200-symbol
    # breadth screen. That stub must never satisfy the freshness check below
    # — otherwise a real fetch (pe/pb/roe/de_ratio/revenue_growth) never runs
    # for the FUNDAMENTALS_TTL window, and every candidate that passed
    # through the screener scores as fundamentals-neutral for a week.
    is_shallow_screen_stub = cached is not None and cached.source == "universe_screen"
    if cached and not is_shallow_screen_stub and _is_fresh(cached.fetched_at, FUNDAMENTALS_TTL):
        return {
            "ticker": ticker,
            "data": json.loads(cached.data_json or "{}"),
            "source": cached.source,
            "stale": False,
        }
    live_result = build_provider_registry(db).get_fundamentals(ticker)
    live_data = live_result.get("data") if live_result.get("ok") else None
    if live_data:
        if cached is None:
            cached = Fundamental(ticker=ticker)
            db.add(cached)
        cached.data_json = json.dumps(live_data, default=str)
        cached.source = live_result.get("provider", "provider")
        cached.fetched_at = datetime.now(UTC)
        cached.stale = False
        db.commit()
        return {
            "ticker": ticker,
            "data": live_data,
            "source": live_result.get("provider", "provider"),
            "stale": False,
            "message": "; ".join(live_result.get("quality", {}).get("warnings", [])) or None,
        }
    if cached:
        return {
            "ticker": ticker,
            "data": json.loads(cached.data_json or "{}"),
            "source": cached.source,
            "stale": True,
            "message": "Returning cached fundamentals because live data is unavailable.",
        }
    return {
        "ticker": ticker,
        "data": {},
        "source": "unavailable",
        "stale": True,
        "message": "; ".join(live_result.get("quality", {}).get("warnings", [])) or "No fundamentals are cached and live data is unavailable.",
    }


def provider_status(db: Session) -> list[dict[str, Any]]:
    return build_provider_registry(db).status()


def macro_indicators(db: Session) -> list[dict[str, Any]]:
    rows = db.query(MacroIndicator).order_by(MacroIndicator.date.desc()).limit(25).all()
    if rows and _is_fresh(max(row.fetched_at for row in rows), MACRO_TTL):
        return [_macro_to_dict(row, stale=False) for row in rows]
    if not rows:
        defaults = [
            MacroIndicator(name="ECB deposit rate", value=Decimal("0"), date=date.today(), source="fallback", stale=True),
            MacroIndicator(name="Euro area CPI", value=Decimal("0"), date=date.today(), source="fallback", stale=True),
        ]
        db.add_all(defaults)
        db.commit()
        rows = defaults
    return [_macro_to_dict(row, stale=True) for row in rows]


def _is_valid_close(value: Any) -> bool:
    """
    Determines whether a value is suitable for caching as a close price.
    
    Returns true if the value is non-null and can be converted to a finite number,
    false otherwise.
    """
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, InvalidOperation):
        return False


def _upsert_price(
    db: Session, ticker: str, item: dict[str, Any], *, commit: bool = True, record_currency: bool = True
) -> None:
    """
    Upsert a price record, skipping if the close value is invalid.
    
    Parameters:
        ticker (str): The stock ticker symbol.
        item (dict[str, Any]): Price data with required "close" and "date" fields; optionally includes "open", "high", "low", "volume", "source", and "currency".
        commit (bool, optional): Whether to commit the transaction immediately. Defaults to True.
    """
    if not _is_valid_close(item.get("close")):
        return
    row = (
        db.query(PriceCache)
        .filter(PriceCache.ticker == ticker, PriceCache.date == item["date"])
        .one_or_none()
    )
    reported = item.get("currency")
    if reported and record_currency:
        from app.foundation.data_backbone.listing_currency import record_listing_currency

        record_listing_currency(db, ticker, reported, f"{item.get('source') or 'provider'}_metadata")
    if row is None:
        from app.foundation.data_backbone.listing_currency import resolve_quote_currency

        row = PriceCache(
            ticker=ticker,
            date=item["date"],
            close=item["close"],
            # A history row without a currency used to be stored as "EUR"
            # (AAPL, SHEL.L): resolve it like every other reader does.
            currency=reported or resolve_quote_currency(db, ticker),
        )
        db.add(row)
    row.open = item.get("open")
    row.high = item.get("high")
    row.low = item.get("low")
    row.close = item["close"]
    row.volume = item.get("volume")
    row.source = item.get("source", "unknown")
    row.currency = reported or row.currency
    row.fetched_at = datetime.now(UTC)
    row.stale = False
    if commit:
        db.commit()


def _as_date(value: Any) -> date | None:
    """A bar's calendar date from a ``date``, ``datetime``/Timestamp or ISO string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _upsert_prices(
    db: Session, ticker: str, items: list[dict[str, Any]], *, commit: bool = True,
) -> int:
    """Batch form of :func:`_upsert_price` for one ticker's whole history.

    The per-bar form issued one SELECT per bar (~1,250 for a 5-year symbol) plus
    a currency lookup per new row. This issues one SELECT for the stored rows of
    the date range, one bulk INSERT for new dates and one bulk UPDATE for known
    ones, and resolves the quote currency once. Plain ORM bulk statements keep it
    dialect-agnostic (SQLite and PostgreSQL).

    The stored result matches the sequential loop: invalid closes are skipped,
    a repeated date keeps its last item, and a known date keeps its stored
    currency unless the provider reports one. Like ``_upsert_price(...,
    record_currency=False)`` it does not touch the listing currency; ``history``
    records the provider-reported one itself. Returns the number of rows written.

    *ticker* must already be upper-cased (``history`` does that). Two writers
    racing on the same ticker can still both see a date as new and the loser's
    INSERT violates ``uq_price_cache_ticker_date`` — the same failure the
    per-bar loop had; the error propagates to the caller.
    """
    from sqlalchemy import insert, update

    by_date: dict[date, dict[str, Any]] = {}
    for item in items:
        if not _is_valid_close(item.get("close")):
            continue
        bar_date = _as_date(item.get("date"))
        if bar_date is None:
            # An unrecognised date shape: keep the per-bar path's behaviour for it.
            _upsert_price(db, ticker, item, commit=False)
            continue
        by_date[bar_date] = item  # last item for a date wins, as a sequential loop would
    if not by_date:
        if commit:
            db.commit()
        return 0

    fetched_at = datetime.now(UTC)
    default_currency: str | None = None

    def _row(item: dict[str, Any], stored_currency: str | None) -> dict[str, Any]:
        nonlocal default_currency
        reported = item.get("currency")
        currency = reported or stored_currency
        if currency is None:
            if default_currency is None:
                from app.foundation.data_backbone.listing_currency import resolve_quote_currency

                # A history row without a currency used to be stored as "EUR"
                # (AAPL, SHEL.L): resolve it like every other reader does.
                default_currency = resolve_quote_currency(db, ticker)
            currency = default_currency
        return {
            "open": item.get("open"),
            "high": item.get("high"),
            "low": item.get("low"),
            "close": item["close"],
            "volume": item.get("volume"),
            "source": item.get("source", "unknown"),
            "currency": currency,
            "fetched_at": fetched_at,
            "stale": False,
        }

    stored = {
        row.date: (row.id, row.currency)
        for row in db.query(PriceCache.id, PriceCache.date, PriceCache.currency).filter(
            PriceCache.ticker == ticker,
            PriceCache.date >= min(by_date),
            PriceCache.date <= max(by_date),
        )
    }
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for bar_date, item in by_date.items():
        known = stored.get(bar_date)
        if known is None:
            inserts.append({"ticker": ticker, "date": bar_date, **_row(item, None)})
        else:
            updates.append({"id": known[0], **_row(item, known[1])})
    if inserts:
        db.execute(insert(PriceCache), inserts)
    if updates:
        db.execute(update(PriceCache), updates)
    if commit:
        db.commit()
    return len(by_date)


def _price_to_dict(row: PriceCache, stale: bool) -> dict[str, Any]:
    return {
        "date": row.date,
        "open": float(row.open) if row.open is not None else None,
        "high": float(row.high) if row.high is not None else None,
        "low": float(row.low) if row.low is not None else None,
        "close": float(row.close),
        "volume": float(row.volume) if row.volume is not None else None,
        "source": row.source,
        "currency": row.currency,
        "stale": stale or row.stale,
    }


def _macro_to_dict(row: MacroIndicator, stale: bool) -> dict[str, Any]:
    return {
        "name": row.name,
        "value": float(row.value),
        "date": row.date,
        "source": row.source,
        "stale": stale or row.stale,
    }
