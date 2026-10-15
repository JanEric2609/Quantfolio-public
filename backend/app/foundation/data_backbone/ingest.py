"""Data ingestion orchestrator for time-series data from multiple providers."""

from datetime import datetime, timedelta, UTC
from decimal import Decimal
import logging
import math
import threading
from typing import Any, cast

import numpy as np
import pandas as pd
from sqlalchemy import exc as sa_exc, text
from sqlalchemy.orm import Session

from app.foundation.models.entities import MacroIndicator
from app.foundation.providers.registry import build_provider_registry

logger = logging.getLogger(__name__)

# Covers classify_and_store's default lookback_days=400 and
# refit_regime_model's lookback_days=750, plus margin — see
# docs/archive/audits/2026-08-26/deepdive-01-discover-engine.md Bug 1.
MACRO_BACKFILL_LOOKBACK_DAYS = 900

# FRED answered VIXCLS with "Internal Server Error" at 07:00 UTC on seven
# consecutive days (2026-09-22..28) while the same request succeeded later in
# the day, so one failed series gets a few spaced retries before it is
# skipped for the day.
MACRO_FETCH_ATTEMPTS = 3
MACRO_RETRY_DELAYS_S = (5.0, 30.0)

# The retries above did not help: every 07:00 VIXCLS request failed from at
# least 2026-09-15 to 09-28 while the identical request succeeded at 20:30,
# and FRED's VIXCLS itself lagged (last updated 09-23, newest value 09-22,
# while T10Y2Y had 09-25). VIXCLS is the CBOE index close, so the days FRED
# lacks are filled from the index's own daily bars; on the 61 days both had,
# the values were identical.
VIX_SERIES_ID = "VIXCLS"
VIX_INDEX_SYMBOL = "^VIX"
#: A stored bar is the quarantined one when its close is within this share.
_SAME_CLOSE = 0.005
#: A split signature is a split only when the provider lists a split within
#: this many sessions of it, at a ratio within this share (the day's own move
#: rides on top of the split ratio). Plan 2026-10-14, M1.
SPLIT_EVENT_SESSIONS = 3
SPLIT_EVENT_RATIO_TOL = 0.10
#: Without a split event, a move up to this factor either way is a real move
#: (a crash or a doubling). A larger one (P500.DE's 100:1) stays a split even
#: when the provider lists no event.
REAL_MOVE_MAX_FACTOR = 2.5
#: A split-event lookup that takes longer is treated as unreadable.
SPLIT_EVENT_TIMEOUT_S = 20.0
#: A timed-out lookup cannot be cancelled; at most this many may still be running.
SPLIT_EVENT_MAX_PENDING = 2
_split_lookups = threading.BoundedSemaphore(SPLIT_EVENT_MAX_PENDING)


def _fetch_split_events(symbol: str) -> Any:
    """yfinance's split series for ``symbol`` (a pandas Series), or None.

    Run in a worker thread with a hard deadline: yfinance's cookie and crumb
    requests can block past any socket timeout, and a re-pull must never hang
    on a lookup that only refines a verdict.

    A thread cannot be cancelled once it runs, so the lookup is a daemon
    thread (it never holds up process exit) and at most
    :data:`SPLIT_EVENT_MAX_PENDING` may still be running: while that many are
    stuck, a new lookup is not started and reads as unknown.
    """
    if not _split_lookups.acquire(blocking=False):
        logger.warning("split events for %s skipped: %d earlier lookups still running",
                       symbol, SPLIT_EVENT_MAX_PENDING)
        return None
    box: dict[str, Any] = {}
    done = threading.Event()

    def _load() -> None:
        try:
            import yfinance as yf

            box["events"] = yf.Ticker(symbol).splits
        except Exception as exc:  # noqa: BLE001 - no events known is not "no split"
            box["error"] = exc
        finally:
            _split_lookups.release()
            done.set()

    threading.Thread(target=_load, name=f"split-events-{symbol}", daemon=True).start()
    if not done.wait(SPLIT_EVENT_TIMEOUT_S):
        logger.warning("split events for %s timed out after %.0f s", symbol, SPLIT_EVENT_TIMEOUT_S)
        return None
    if "error" in box:
        logger.debug("split events unavailable for %s", symbol, exc_info=box["error"])
        return None
    return box.get("events")


def _finite(value: Any) -> float:
    """``value`` as a float; 0.0 for None, NaN or anything unreadable."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


class DataIngester:
    """Orchestrates data ingestion from providers into hypertables."""

    def __init__(self, db: Session):
        self.db = db
        self.registry = build_provider_registry(db)

    def _resolve_currency(self, symbol: str) -> str:
        """Quote unit for bars a provider returned without one.

        Used to read the ``assets`` table, whose currency is a fund's base
        currency (EUNL.DE: USD, although Xetra quotes it in EUR), and to
        default everything else to "EUR" (AAPL, SHEL.L). See
        :mod:`app.foundation.data_backbone.listing_currency`.
        """
        from app.foundation.data_backbone.listing_currency import resolve_quote_currency

        return resolve_quote_currency(self.db, symbol)

    # ------------------------------------------------------------------
    # Bars: one pinned provider per series, guarded on ingest (plan
    # 2026-10-12, D1/D2).
    # ------------------------------------------------------------------

    def _pin(self, symbol: str) -> Any:
        from app.foundation.models.entities import BarSeriesMeta

        try:
            return self.db.get(BarSeriesMeta, symbol)
        except Exception:  # noqa: BLE001 - a missing table must not stop ingestion
            logger.debug("bar pin lookup failed for %s", symbol, exc_info=True)
            try:
                self.db.rollback()
            except Exception:
                pass
            return None

    def _set_pin(
        self, symbol: str, provider: str, *, full_repull: bool = False, quarantine_reason: str | None = None,
        clear_quarantine: bool = False,
    ) -> Any:
        """Create or move the pin and return it. Does not commit."""
        from app.foundation.models.entities import BarSeriesMeta

        from app.foundation.providers.registry import ProviderRegistry

        adjusted = ProviderRegistry.is_adjusted_history_provider(provider)
        pin = self.db.get(BarSeriesMeta, symbol)
        now = datetime.now(UTC)
        if pin is None:
            pin = BarSeriesMeta(symbol=symbol, provider=provider, adjusted=adjusted, updated_at=now)
            self.db.add(pin)
        pin.provider = provider
        pin.adjusted = adjusted
        pin.updated_at = now
        if full_repull:
            pin.last_full_repull_at = now
        if quarantine_reason is not None:
            pin.quarantine_reason = quarantine_reason
        elif clear_quarantine:
            pin.quarantine_reason = None
        return pin

    def _bars_frame(self, symbol: str, result: dict[str, Any]) -> tuple[pd.DataFrame, str, str | None, int]:
        """Provider rows as a validated bar frame: (frame, provider, reported currency, rows received)."""
        data = result.get("data", [])
        provider: str = result.get("provider") or "unknown"
        df: pd.DataFrame = pd.DataFrame(data)
        if df.empty:
            return df, provider, None, 0

        # Standardize columns
        if "date" in df.columns:
            df.rename(columns={"date": "ts"}, inplace=True)
        if "timestamp" in df.columns and "ts" not in df.columns:
            df.rename(columns={"timestamp": "ts"}, inplace=True)

        df["ts"] = pd.to_datetime(df["ts"])
        df["symbol"] = symbol
        df["provider"] = provider
        # The quote unit the provider reports for this series (yfinance does,
        # from the history response's metadata) is recorded for every reader
        # and stamped on the bars; without one, listing_currency resolves it.
        reported = None
        if "currency" in df.columns:
            known = df["currency"].dropna()
            reported = str(known.iloc[-1]) if not known.empty else None
        if reported:
            from app.foundation.data_backbone.listing_currency import record_listing_currency

            record_listing_currency(self.db, symbol, reported, f"{provider}_metadata")
            df["currency"] = df["currency"].fillna(reported)
        else:
            df["currency"] = self._resolve_currency(symbol)

        # Select relevant columns
        cols = ["symbol", "ts", "open", "high", "low", "close", "volume", "currency", "provider"]
        df = cast(pd.DataFrame, df.loc[:, [c for c in cols if c in df.columns]])

        initial_count = len(df)

        if "close" in df.columns:
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            nan_mask = df["close"].isna()
            if bool(nan_mask.any()):
                nan_count = int(nan_mask.sum())
                logger.warning(
                    "Symbol %s: rejecting %d rows with NaN close prices",
                    symbol, nan_count,
                )
                df = cast(pd.DataFrame, df[~nan_mask])

        # Reject negative prices (invalid data)
        for price_col in ("open", "high", "low", "close"):
            if price_col in df.columns:
                df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
                neg_mask = df[price_col] < 0
                if bool(neg_mask.any()):
                    neg_count = int(neg_mask.sum())
                    logger.warning(
                        "Symbol %s: rejecting %d rows with negative %s prices",
                        symbol, neg_count, price_col,
                    )
                    df = cast(pd.DataFrame, df[~neg_mask])

        # Reject zero close prices (data error)
        if "close" in df.columns:
            zero_mask = df["close"] == 0
            if bool(zero_mask.any()):
                zero_count = int(zero_mask.sum())
                logger.warning(
                    "Symbol %s: rejecting %d rows with zero close prices",
                    symbol, zero_count,
                )
                df = cast(pd.DataFrame, df[~zero_mask])
        return df, provider, reported, initial_count

    def _stored_tail(self, symbol: str, before: Any) -> pd.DataFrame:
        """The two newest stored bars before ``before`` (what the guard measures against)."""
        rows = self.db.execute(
            text(
                "SELECT ts, open, high, low, close, provider FROM bar_prices "
                "WHERE symbol = :symbol AND ts < :before ORDER BY ts DESC LIMIT 2"
            ),
            {"symbol": symbol, "before": before},
        ).fetchall()
        return pd.DataFrame(
            [tuple(r) for r in rows], columns=pd.Index(["ts", "open", "high", "low", "close", "provider"]),
        )

    def _write_quarantine(self, symbol: str, bars: list[Any]) -> None:
        """Store quarantined bars; a stored bar moved to quarantine leaves bar_prices. Does not commit."""
        from app.foundation.models.entities import BarQuarantine

        for q in bars:
            ts = q.ts.to_pydatetime().replace(tzinfo=None)
            existing = (
                self.db.query(BarQuarantine)
                .filter(BarQuarantine.symbol == symbol, BarQuarantine.ts == ts, BarQuarantine.provider == q.provider)
                .first()
            )
            if existing is None:
                self.db.add(BarQuarantine(
                    symbol=symbol, ts=ts, open=q.open, high=q.high, low=q.low, close=q.close,
                    provider=q.provider, reason=q.reason, detected_at=datetime.now(UTC),
                ))
            else:
                existing.close = q.close
                existing.reason = q.reason
                existing.resolved_at = None
                existing.resolution = None
            # The stored copy of this bar leaves bar_prices, also when the
            # incremental fetch's one-day overlap brought it back as "new"
            # (plan 2026-10-14, H3). Only a row with the quarantined close goes:
            # a fallback provider's bad fresh bar must not delete the good
            # pinned bar stored for the same day.
            self.db.execute(
                text(
                    "DELETE FROM bar_prices WHERE symbol = :symbol AND ts = :ts "
                    "AND abs(close - :close) <= :tol * abs(:close)"
                ),
                {"symbol": symbol, "ts": q.ts.to_pydatetime(), "close": float(q.close), "tol": _SAME_CLOSE},
            )
            logger.warning("bar_guard: %s %s quarantined (%s, %s)", symbol, q.ts.date(), q.reason, q.provider)

    def _confirm_quarantined(self, symbol: str, pinned: str, bars: list[Any]) -> list[Any]:
        """Bars a second adjusted provider confirms (same date, close within 1 %).

        Only spikes and scale breaks are worth a second call; a non-session
        bar or a split stays quarantined whatever another provider says.
        """
        from app.foundation.data_backbone.bar_guard import confirms

        from app.foundation.providers.registry import ProviderRegistry
        from app.foundation.providers.tiingo_provider import unsupported_listing

        candidates = [q for q in bars if q.reason in ("spike_reversal", "provider_scale")]
        if not candidates:
            return []
        others = [
            name for name in ProviderRegistry._PRICE_HISTORY_PROVIDER_ORDER
            if name != pinned and name != candidates[0].provider
            # Tiingo lists no European listings: asking it for a .DE or .L symbol
            # spends its 50-an-hour budget on a certain miss (plan 2026-10-14, E3).
            # Its own rule keeps US share classes (BRK.B) and .SS/.SZ listings.
            and not (name == "tiingo" and unsupported_listing(symbol))
        ]
        start = (min(q.ts for q in candidates) - pd.Timedelta(days=3)).date().isoformat()
        end = (max(q.ts for q in candidates) + pd.Timedelta(days=3)).date().isoformat()
        for name in others:
            try:
                res = self.registry.get_price_history(symbol, start=start, end=end, provider=name)
            except Exception:  # noqa: BLE001
                continue
            if not res.get("ok"):
                continue
            closes: dict[Any, float] = {}
            for row in res.get("data") or []:
                day = row.get("date")
                if day is None or row.get("close") is None:
                    continue
                closes[pd.Timestamp(day).date()] = float(row["close"])
            return [q for q in candidates if confirms(q.close, closes.get(q.ts.date()))]
        return []

    def _upsert_bars(self, df: pd.DataFrame, provider: str) -> None:
        """Insert-or-update ``df`` into bar_prices. Does not commit."""
        params_list: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            ts_val = cast(pd.Timestamp, pd.Timestamp(cast(Any, row["ts"]))).to_pydatetime()
            params_list.append({
                "symbol": row["symbol"],
                "ts": ts_val,
                "open": _finite(row.get("open")),
                "high": _finite(row.get("high")),
                "low": _finite(row.get("low")),
                "close": _finite(row.get("close")),
                # NaN is truthy, so ``or 0`` let int(nan) raise and roll back the
                # symbol's whole batch (plan 2026-10-14, L2). The column is NOT
                # NULL: an unknown volume is 0, as a missing one already was.
                "volume": int(_finite(row.get("volume"))),
                "currency": row["currency"],
                "provider": row.get("provider") or provider,
            })

        # ON CONFLICT must name the (symbol, ts) unique index (migration 0075).
        # The previous target-less DO NOTHING never fired because the only
        # unique key was (id, ts) with an auto-generated id, so re-ingesting a
        # symbol (daily 1-day overlap, and the discover 5-year re-pull) silently
        # inserted duplicate bars that corrupt every bar-derived statistic.
        sql = text("""
            INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            ON CONFLICT (symbol, ts) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                currency = EXCLUDED.currency,
                provider = EXCLUDED.provider
        """)
        BATCH_SIZE = 500
        for i in range(0, len(params_list), BATCH_SIZE):
            batch = params_list[i:i + BATCH_SIZE]
            self.db.execute(sql, batch)

    def ingest_bar_prices(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """Ingest OHLCV data for a symbol.

        Asks only the symbol's pinned provider (``bar_series_meta``); a
        series without a pin is pinned to the provider the adjusted chain
        answers with. A failing pinned provider is reported, not replaced:
        switching provider mid-series is what spliced SPY/URTH/VGK, so a
        switch goes through :meth:`repull_series`. New bars pass
        :func:`~app.foundation.data_backbone.bar_guard.guard_bars` first.

        Args:
            symbol: Ticker symbol (e.g., 'SPY', 'DBX1.DE').
            start_date: ISO format start date.
            end_date: ISO format end date.
            days: Days back from today (if start/end not provided).

        Returns:
            Dict with counts and provider info.
        """
        pin = self._pin(symbol)
        pinned: str | None = pin.provider if pin is not None else None
        if pinned:
            result = self.registry.get_price_history(
                symbol, start=start_date, end=end_date, days=days, provider=pinned,
            )
        else:
            result = self.registry.get_price_history(symbol, start=start_date, end=end_date, days=days)

        if not result.get("ok"):
            # Record failure to provider_health_history
            self._record_provider_health(
                provider=pinned or result.get("provider", "unknown"),
                capability="bar_prices",
                ok=False,
                message=result.get("error"),
            )
            return {
                "success": False,
                "symbol": symbol,
                "message": (
                    f"pinned provider {pinned} failed (no fallback mid-series): {result.get('error')}"
                    if pinned else result.get("error")
                ),
            }

        df, provider, reported, initial_count = self._bars_frame(symbol, result)
        if initial_count == 0:
            return {
                "success": False,
                "symbol": symbol,
                "message": "No data returned",
            }

        if df.empty:
            logger.warning("Symbol %s: all %d rows rejected by validation", symbol, initial_count)
            return {
                "success": False,
                "symbol": symbol,
                "message": f"All {initial_count} rows rejected by data validation",
            }

        from app.foundation.data_backbone.bar_guard import guard_bars, guard_class_for

        try:
            stored = self._stored_tail(symbol, cast(pd.Timestamp, df["ts"].min()).to_pydatetime())
        except Exception:  # noqa: BLE001
            logger.debug("bar_guard: stored tail unavailable for %s", symbol, exc_info=True)
            stored = None
        guarded = guard_bars(
            df, stored, symbol=symbol, guard_class=guard_class_for(self.db, symbol),
            pinned_provider=pinned or provider,
        )
        if guarded.split_suspected:
            logger.warning("bar_guard: %s shows a split signature; re-pulling the series", symbol)
            return self.repull_series(symbol, provider=pinned or provider)
        released: list[Any] = []
        if guarded.quarantined:
            released = self._confirm_quarantined(symbol, pinned or provider, guarded.quarantined)
        keep = cast(pd.DataFrame, guarded.accepted)
        if released:
            released_days = {q.ts.date() for q in released}
            back = cast(pd.DataFrame, df[df["ts"].dt.date.isin(released_days)])
            keep = cast(pd.DataFrame, pd.concat([keep, back], ignore_index=True))
        quarantined = [q for q in guarded.quarantined if q not in released]

        rejected = initial_count - len(keep)
        if rejected > 0:
            logger.info(
                "Symbol %s: validated %d/%d rows (%d rejected)",
                symbol, len(keep), initial_count, rejected,
            )

        # Insert into bar_prices hypertable (batched for performance)
        try:
            self._write_quarantine(symbol, quarantined)
            if released:
                self._mark_released(symbol, released)
            if not keep.empty:
                self._upsert_bars(keep, provider)
            if not pinned:
                self._set_pin(symbol, provider)
            if reported:
                # Older rows of the symbol may carry a guessed label; one
                # series has one quote unit.
                from app.foundation.data_backbone.listing_currency import relabel_stored_prices

                relabel_stored_prices(self.db, symbol, reported)
            self.db.commit()

            self._record_provider_health(
                provider=provider,
                capability="bar_prices",
                ok=True,
                message=f"Ingested {len(keep)} bars for {symbol}",
                latency_ms=0,
            )

            if keep.empty:
                return {
                    "success": False,
                    "symbol": symbol,
                    "message": f"All {initial_count} rows quarantined by the bar guard",
                    "quarantined": len(quarantined),
                }
            return {
                "success": True,
                "symbol": symbol,
                "rows_inserted": len(keep),
                "quarantined": len(quarantined),
                "provider": provider,
                "date_range": {
                    "start": str(keep["ts"].min()),
                    "end": str(keep["ts"].max()),
                },
            }

        except (sa_exc.IntegrityError, sa_exc.ProgrammingError) as e:
            # Schema-shape failures (NotNullViolation, InvalidColumnReference,
            # a missing ON CONFLICT target) mean bar_prices itself is broken,
            # not that this one provider call failed — see ADR 0014 §1: this
            # exact defect regressed silently once before because it was
            # logged indistinguishably from an ordinary provider outage.
            logger.error(
                "Schema error ingesting bars for %s — bar_prices write path may be "
                "broken (id default / unique index missing): %s",
                symbol, e,
            )
            try:
                self.db.rollback()
            except Exception:
                pass
            self._record_provider_health(
                provider=provider,
                capability="bar_prices",
                ok=False,
                message=f"schema_error: {e}",
            )
            return {
                "success": False,
                "symbol": symbol,
                "message": str(e),
                "error_type": "schema",
            }
        except Exception as e:
            logger.error(f"Error ingesting bars for {symbol}: {e}")
            try:
                self.db.rollback()
            except Exception:
                pass
            self._record_provider_health(
                provider=provider,
                capability="bar_prices",
                ok=False,
                message=str(e),
            )
            return {
                "success": False,
                "symbol": symbol,
                "message": str(e),
                "error_type": "provider",
            }

    def _mark_released(self, symbol: str, released: list[Any]) -> None:
        """Record that a second provider confirmed these bars. Does not commit."""
        from app.foundation.models.entities import BarQuarantine

        now = datetime.now(UTC)
        for q in released:
            ts = q.ts.to_pydatetime().replace(tzinfo=None)
            row = (
                self.db.query(BarQuarantine)
                .filter(BarQuarantine.symbol == symbol, BarQuarantine.ts == ts, BarQuarantine.provider == q.provider)
                .first()
            )
            if row is None:
                row = BarQuarantine(
                    symbol=symbol, ts=ts, open=q.open, high=q.high, low=q.low, close=q.close,
                    provider=q.provider, reason=q.reason, detected_at=now,
                )
                self.db.add(row)
            row.resolved_at = now
            row.resolution = "confirmed:second_provider"

    #: Default window of a full re-pull when nothing is stored yet.
    REPULL_DEFAULT_DAYS = 365 * 5 + 30

    def repull_series(
        self, symbol: str, provider: str | None = None, *, start_date: str | None = None,
    ) -> dict[str, Any]:
        """Replace a symbol's whole stored series with one provider's, in one transaction.

        The only way a series changes provider (D1). The window starts at the
        oldest stored bar (or ``start_date``), so coverage never shrinks. A
        split signature that survives the re-pull is stored as it is but
        marks the series ``split_unadjusted``, which keeps the symbol out of
        Discover. Cached closes for the symbol (``price_cache``) are dropped,
        since they sit on the old scale.
        """
        from app.foundation.data_backbone.bar_guard import guard_bars, guard_class_for

        if start_date is None:
            oldest = self.db.execute(
                text("SELECT min(ts) FROM bar_prices WHERE symbol = :symbol"), {"symbol": symbol},
            ).scalar()
            if oldest is not None:
                start_date = cast(pd.Timestamp, pd.Timestamp(oldest)).date().isoformat()
        if start_date is None:
            start_date = (datetime.now(UTC).date() - timedelta(days=self.REPULL_DEFAULT_DAYS)).isoformat()

        if provider is None:
            result = self.registry.get_price_history(symbol, start=start_date)
        else:
            result = self.registry.get_price_history(symbol, start=start_date, provider=provider)
        if not result.get("ok"):
            self._record_provider_health(
                provider=provider or str(result.get("provider") or "unknown"),
                capability="bar_prices",
                ok=False,
                message=f"repull: {result.get('error')}",
            )
            return {"success": False, "symbol": symbol, "message": result.get("error"), "repull": True}

        df, used, reported, initial_count = self._bars_frame(symbol, result)
        if df.empty:
            return {"success": False, "symbol": symbol, "message": "No data returned", "repull": True}
        short = self._repull_too_short(symbol, start_date, df)
        if short:
            # A truncated answer (delisted ticker, partial window, rate limit)
            # must not replace a longer stored series: the delete below would
            # lose the history. AVB returned 1 bar for a 27-bar window.
            logger.warning("repull of %s skipped: %s", symbol, short)
            self._record_provider_health(
                provider=used, capability="bar_prices", ok=False, message=f"repull: {short}",
            )
            return {"success": False, "symbol": symbol, "message": short, "repull": True}
        guarded = guard_bars(
            df, None, symbol=symbol, guard_class=guard_class_for(self.db, symbol),
            pinned_provider=used, allow_split=True,
        )
        keep = cast(pd.DataFrame, guarded.accepted)
        verdict = self.split_verdict(symbol, guarded)
        unadjusted = verdict in ("split", "unknown")
        try:
            self.db.execute(text("DELETE FROM bar_prices WHERE symbol = :symbol"), {"symbol": symbol})
            self._write_quarantine(symbol, guarded.quarantined)
            if not keep.empty:
                self._upsert_bars(keep, used)
            pin = self._set_pin(
                symbol, used, full_repull=True,
                quarantine_reason="split_unadjusted" if unadjusted else None,
                clear_quarantine=not unadjusted,
            )
            if verdict == "real_move":
                days = ", ".join(f"{d.date()} x{r:.2f}" for d, r in guarded.split_days)
                pin.notes = f"real_move: {days} (no split event at the provider)"
            from app.foundation.models.entities import PriceCache

            self.db.query(PriceCache).filter(PriceCache.ticker == symbol).delete(synchronize_session=False)
            if reported:
                from app.foundation.data_backbone.listing_currency import relabel_stored_prices

                relabel_stored_prices(self.db, symbol, reported)
            self.db.commit()
        except Exception as e:  # noqa: BLE001
            logger.error("repull of %s failed: %s", symbol, e)
            try:
                self.db.rollback()
            except Exception:
                pass
            return {"success": False, "symbol": symbol, "message": str(e), "repull": True}
        if verdict == "real_move":
            logger.warning("bar_guard: %s real_move kept: %s", symbol, guarded.split_days)
        elif unadjusted:
            logger.warning(
                "bar_guard: %s still shows a split after a full re-pull from %s (%s)", symbol, used, verdict,
            )
        self._record_provider_health(
            provider=used, capability="bar_prices", ok=True,
            message=f"Re-pulled {len(keep)} bars for {symbol}",
        )
        return {
            "success": not keep.empty,
            "symbol": symbol,
            "repull": True,
            "rows_inserted": len(keep),
            "rows_received": initial_count,
            "quarantined": len(guarded.quarantined),
            "provider": used,
            "split_unadjusted": unadjusted,
            "split_verdict": verdict,
        }

    def split_events(self, symbol: str) -> list[tuple[pd.Timestamp, float]] | None:
        """The provider's split events (day, factor), or None when they cannot be read.

        yfinance's factor is new shares per old one (2.0 for a 2:1 split, 0.1
        for a 1:10 reverse split), so the price ratio across it is 1 / factor.
        Called only on the re-pull path.
        """
        splits = _fetch_split_events(symbol)
        if splits is None:
            return None
        out: list[tuple[pd.Timestamp, float]] = []
        for ts, factor in splits.items():
            try:
                f = float(factor)
            except (TypeError, ValueError):
                continue
            if f > 0:
                day = cast(pd.Timestamp, pd.Timestamp(cast(Any, ts)))
                out.append(((day.tz_localize(None) if day.tzinfo is not None else day).normalize(), f))
        return out

    def split_verdict(self, symbol: str, guarded: Any) -> str:
        """What a split signature in a re-pulled series is (plan 2026-10-14, M1).

        ``none``: no signature. ``split``: the provider lists a split at that
        day and ratio, so the series is unadjusted. ``real_move``: no event and
        a move a market can make. ``unknown``: the events cannot be read, kept
        as a split as before.
        """
        if not guarded.split_suspected:
            return "none"
        events = self.split_events(symbol)
        if events is None:
            return "unknown"
        for day, ratio in guarded.split_days:
            for event_day, factor in events:
                near = abs(int(np.busday_count(min(day, event_day).date(), max(day, event_day).date())))
                if near <= SPLIT_EVENT_SESSIONS and abs(ratio * factor - 1.0) <= SPLIT_EVENT_RATIO_TOL:
                    return "split"
        if all(1.0 / REAL_MOVE_MAX_FACTOR <= ratio <= REAL_MOVE_MAX_FACTOR for _, ratio in guarded.split_days):
            return "real_move"
        return "split"

    #: A re-pull must return at least this share of the stored bars in its
    #: window, and start within REPULL_START_SLACK_DAYS of the stored start.
    REPULL_MIN_COVERAGE = 0.9
    REPULL_START_SLACK_DAYS = 10

    def _repull_too_short(self, symbol: str, start_date: str, df: pd.DataFrame) -> str | None:
        """Why a re-pulled frame covers clearly less than the stored series, or None."""
        row = self.db.execute(
            text("SELECT count(*), min(ts) FROM bar_prices WHERE symbol = :symbol AND ts >= :start"),
            {"symbol": symbol, "start": start_date},
        ).fetchone()
        if row is None or not row[0]:
            return None
        stored, oldest_raw = int(row[0]), row[1]
        received = len(df)
        if received < self.REPULL_MIN_COVERAGE * stored:
            return f"provider returned {received} bars for a window holding {stored}; stored series kept"
        oldest = cast(pd.Timestamp, pd.Timestamp(oldest_raw)).tz_localize(None)
        first = cast(pd.Timestamp, pd.Timestamp(cast(Any, df["ts"].min()))).tz_localize(None)
        if first - oldest > pd.Timedelta(days=self.REPULL_START_SLACK_DAYS):
            return f"provider starts {first.date()}, stored series starts {oldest.date()}; stored series kept"
        return None

    @staticmethod
    def _fetch_macro_history(provider: Any, series_id: str, start: str, end: str) -> dict[str, Any]:
        """``provider.get_history`` with MACRO_FETCH_ATTEMPTS spaced attempts."""
        import time

        result: dict[str, Any] = {}
        for attempt in range(MACRO_FETCH_ATTEMPTS):
            result = provider.get_history(series_id, start=start, end=end)
            if result.get("ok"):
                return result
            if attempt < len(MACRO_RETRY_DELAYS_S):
                logger.info(
                    "macro backfill: %s attempt %d failed (%s), retrying",
                    series_id, attempt + 1, result.get("error"),
                )
                time.sleep(MACRO_RETRY_DELAYS_S[attempt])
        return result

    def _fill_vix_from_index(self, start_date: Any, end_date: Any) -> dict[str, Any]:
        """Store ``VIXCLS`` for the days after FRED's newest value from ``^VIX`` bars.

        Rows carry ``source="cboe_index"``; the classifier's macro frame takes
        one value per date, and on shared dates the two agree. Does not
        commit; never raises.
        """
        from sqlalchemy import func

        try:
            self.db.flush()  # the FRED rows of this refresh are not committed yet
            last_fred = (
                self.db.query(func.max(MacroIndicator.date))
                .filter(MacroIndicator.name == VIX_SERIES_ID, MacroIndicator.source == "fred")
                .scalar()
            )
            since = max(start_date, last_fred + timedelta(days=1)) if last_fred else start_date
            if since > end_date:
                return {"added": 0, "since": since.isoformat()}
            from app.foundation.providers.fred_provider import FredProvider

            hist = self.registry.first_success(
                "get_history",
                VIX_INDEX_SYMBOL,
                start=since.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat(),
                providers=[p for p in self.registry.providers if not isinstance(p, FredProvider)],
            )
            if not hist.get("ok"):
                return {"added": 0, "error": hist.get("error")}
            added = 0
            for row in hist.get("data") or []:
                day, close = row.get("date"), row.get("close")
                if day is None or close is None or not (since <= day <= end_date):
                    continue
                existing = self.db.query(MacroIndicator).filter(
                    MacroIndicator.name == VIX_SERIES_ID,
                    MacroIndicator.date == day,
                    MacroIndicator.source == "cboe_index",
                ).first()
                if existing is not None:
                    existing.value = Decimal(str(float(close)))
                    existing.fetched_at = datetime.now(UTC)
                    continue
                self.db.add(MacroIndicator(
                    name=VIX_SERIES_ID,
                    value=Decimal(str(float(close))),
                    date=day,
                    source="cboe_index",
                    fetched_at=datetime.now(UTC),
                    stale=False,
                ))
                added += 1
            logger.info("macro backfill: VIXCLS filled from %s for %d day(s) since %s", VIX_INDEX_SYMBOL, added, since)
            return {"added": added, "since": since.isoformat()}
        except Exception as exc:  # noqa: BLE001 - the FRED rows must still commit
            logger.warning("macro backfill: VIX index fill failed: %s", exc)
            return {"added": 0, "error": str(exc)}

    def ingest_macro_indicators(self, series_ids: list[str] | None = None) -> dict[str, Any]:
        """Ingest macro indicators from FRED, ECB, Bundesbank.

        Args:
            series_ids: Explicit FRED series IDs to fetch (e.g. the regime
                classifier needs VIXCLS/T10Y2Y/BAA10Y, and only VIXCLS is in
                FredProvider.get_macro_indicators' own default list). When
                given, this routes to the FRED provider specifically rather
                than through the generic multi-provider fallback chain —
                these are FRED series codes, so falling over to ECB/openbb
                with them would either error or silently mean nothing.
                Omit to use the generic chain with each provider's own
                default series list.

        Returns:
            Dict with ingestion results per provider.
        """
        result: dict[str, Any] = {}

        if series_ids is not None:
            from app.foundation.providers.fred_provider import FredProvider

            fred_provider = next(
                (p for p in self.registry.providers if isinstance(p, FredProvider)), None
            )
            if fred_provider is None or not fred_provider.enabled:
                return {"fred": {"success": False, "error": "FRED provider not configured"}}

            # Pull each series' FULL history (not just the latest point) so the
            # regime classifier's rolling-window features accumulate enough rows
            # to clear MIN_CLEAN_ROWS_CLASSIFY in one pass instead of one row/day.
            # get_macro_indicators() is intentionally NOT reused here — it only
            # ever returns the latest value per series, and two other live
            # callers (settings "test connection" probe, quant/market dashboard
            # snapshot) depend on that latest-value shape. get_history() already
            # returns full history correctly and is reused here unmodified.
            actual_source = "fred"
            end_date = datetime.now(UTC).date()
            start_date = end_date - timedelta(days=MACRO_BACKFILL_LOOKBACK_DAYS)
            series_count = 0
            try:
                for series_id in series_ids:
                    hist_result = self._fetch_macro_history(
                        fred_provider, series_id, start_date.isoformat(), end_date.isoformat()
                    )
                    if not hist_result.get("ok"):
                        logger.warning(
                            "macro backfill: %s unavailable: %s",
                            series_id, hist_result.get("error"),
                        )
                        continue
                    for row in hist_result.get("data", []):
                        try:
                            value = row.get("value") if isinstance(row, dict) else row
                            float_value = float(value) if value is not None else 0.0
                            decimal_value = Decimal(str(float_value))

                            date_str = row.get("date") if isinstance(row, dict) else None
                            if date_str:
                                try:
                                    parsed_date = datetime.fromisoformat(
                                        date_str.replace("Z", "+00:00")
                                    ).date()
                                except (ValueError, AttributeError):
                                    parsed_date = datetime.now(UTC).date()
                            else:
                                parsed_date = datetime.now(UTC).date()

                            # Check for existing record with same (name, date, source)
                            existing = self.db.query(MacroIndicator).filter(
                                MacroIndicator.name == series_id,
                                MacroIndicator.date == parsed_date,
                                MacroIndicator.source == actual_source,
                            ).first()

                            if existing:
                                # Update existing record
                                existing.value = decimal_value
                                existing.fetched_at = datetime.now(UTC)
                            else:
                                # Insert new record
                                macro_indicator = MacroIndicator(
                                    name=series_id,
                                    value=decimal_value,
                                    date=parsed_date,
                                    source=actual_source,
                                    fetched_at=datetime.now(UTC),
                                    stale=False,
                                )
                                self.db.add(macro_indicator)
                        except Exception as e:
                            logger.warning(
                                "Error ingesting macro %s (%s): %s", series_id, actual_source, e
                            )
                    series_count += 1

                if VIX_SERIES_ID in series_ids:
                    result["vix_index"] = self._fill_vix_from_index(start_date, end_date)
                self.db.commit()
                result[actual_source] = {"success": True, "series_count": series_count}
            except Exception as e:
                logger.error(f"Error in macro backfill transaction: {e}")
                self.db.rollback()
                result[actual_source] = {"success": False, "error": str(e)}

            return result

        # Generic multi-provider chain path (e.g. POST /api/data/macro/refresh with
        # no explicit series_ids) — unchanged: latest-value-per-series shape.
        fred_result = self.registry.first_success("get_macro_indicators")
        # first_success may transparently fail over from FRED to ECB/Bundesbank;
        # record the provider that actually returned the data, not a hardcoded
        # "fred", so provenance and the dedupe key are correct.
        actual_source = str(fred_result.get("provider") or "fred")
        if fred_result.get("ok"):
            fred_data = fred_result.get("data", {})
            try:
                for series_id, series_data in fred_data.items():
                    try:
                        # Parse value
                        value = series_data.get("value") if isinstance(series_data, dict) else series_data
                        float_value = float(value) if value is not None else 0.0
                        decimal_value = Decimal(str(float_value))

                        # Parse date from timestamp or use today
                        timestamp_str = series_data.get("timestamp") if isinstance(series_data, dict) else None
                        if timestamp_str:
                            try:
                                parsed_date = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")).date()
                            except (ValueError, AttributeError):
                                parsed_date = datetime.now(UTC).date()
                        else:
                            parsed_date = datetime.now(UTC).date()

                        # Check for existing record with same (name, date, source)
                        existing = self.db.query(MacroIndicator).filter(
                            MacroIndicator.name == series_id,
                            MacroIndicator.date == parsed_date,
                            MacroIndicator.source == actual_source,
                        ).first()

                        if existing:
                            # Update existing record
                            existing.value = decimal_value
                            existing.fetched_at = datetime.now(UTC)
                        else:
                            # Insert new record
                            macro_indicator = MacroIndicator(
                                name=series_id,
                                value=decimal_value,
                                date=parsed_date,
                                source=actual_source,
                                fetched_at=datetime.now(UTC),
                                stale=False,
                            )
                            self.db.add(macro_indicator)
                    except Exception as e:
                        logger.warning("Error ingesting macro %s (%s): %s", series_id, actual_source, e)

                self.db.commit()
                result[actual_source] = {"success": True, "series_count": len(fred_data)}
            except Exception as e:
                logger.error(f"Error in macro ingest transaction: {e}")
                self.db.rollback()
                result[actual_source] = {"success": False, "error": str(e)}
        else:
            result[actual_source] = {"success": False, "error": fred_result.get("error")}

        return result

    def _record_provider_health(
        self,
        provider: str,
        capability: str,
        ok: bool,
        message: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        """Record provider health event."""
        try:
            sql = text("""
                INSERT INTO provider_health_history (provider, ts, capability, ok, latency_ms, message)
                VALUES (:provider, :ts, :capability, :ok, :latency_ms, :message)
            """)
            self.db.execute(
                sql,
                {
                    "provider": provider,
                    "ts": datetime.now(UTC),
                    "capability": capability,
                    "ok": ok,
                    "latency_ms": latency_ms,
                    "message": message,
                },
            )
            self.db.commit()
        except Exception as e:
            logger.warning(f"Error recording provider health: {e}")
            # A failed INSERT aborts the whole Postgres transaction; without
            # this rollback every later query on the shared session dies with
            # InFailedSqlTransaction (health recording must never poison the
            # caller's session — it is best-effort observability).
            try:
                self.db.rollback()
            except Exception:
                pass
