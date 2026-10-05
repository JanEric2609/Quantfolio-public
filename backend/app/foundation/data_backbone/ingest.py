"""Data ingestion orchestrator for time-series data from multiple providers."""

from datetime import datetime, timedelta, UTC
from decimal import Decimal
import logging
from typing import Any, cast

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

    def ingest_bar_prices(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """Ingest OHLCV data for a symbol.

        Args:
            symbol: Ticker symbol (e.g., 'SPY', 'DBX1.DE').
            start_date: ISO format start date.
            end_date: ISO format end date.
            days: Days back from today (if start/end not provided).

        Returns:
            Dict with counts and provider info.
        """
        # ADR 0014 §7: bar_prices ingestion routes through quant-grade sources
        # (tiingo/twelvedata/databento/eod) first, not the general chain's
        # yfinance-preferred order — get_history() stays the right call for
        # quotes/dossiers/etc, which deliberately prefer yfinance (ADR 0010).
        result = self.registry.get_price_history(symbol, start=start_date, end=end_date, days=days)

        if not result.get("ok"):
            # Record failure to provider_health_history
            self._record_provider_health(
                provider=result.get("provider", "unknown"),
                capability="bar_prices",
                ok=False,
                message=result.get("error"),
            )
            return {
                "success": False,
                "symbol": symbol,
                "message": result.get("error"),
            }

        # Parse data
        data = result.get("data", [])
        provider: str = result.get("provider") or "unknown"

        if not data:
            return {
                "success": False,
                "symbol": symbol,
                "message": "No data returned",
            }

        # Convert to DataFrame
        df: pd.DataFrame = pd.DataFrame(data)

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

        # Warn on extreme daily price changes (>50% day-over-day)
        if "close" in df.columns and len(df) > 1:
            sorted_df = cast(pd.DataFrame, df.sort_values("ts"))
            pct_change = sorted_df["close"].pct_change().abs()
            extreme_mask = pct_change > 0.5
            if bool(extreme_mask.any()):
                extreme_count = int(extreme_mask.sum())
                logger.warning(
                    "Symbol %s: %d rows with extreme daily price change (>50%%)",
                    symbol, extreme_count,
                )

        if df.empty:
            logger.warning("Symbol %s: all %d rows rejected by validation", symbol, initial_count)
            return {
                "success": False,
                "symbol": symbol,
                "message": f"All {initial_count} rows rejected by data validation",
            }

        rejected = initial_count - len(df)
        if rejected > 0:
            logger.info(
                "Symbol %s: validated %d/%d rows (%d rejected)",
                symbol, len(df), initial_count, rejected,
            )

        # Insert into bar_prices hypertable (batched for performance)
        try:
            params_list: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                ts_val = cast(pd.Timestamp, row["ts"]).to_pydatetime()
                params_list.append({
                    "symbol": row["symbol"],
                    "ts": ts_val,
                    "open": float(row.get("open") or 0),
                    "high": float(row.get("high") or 0),
                    "low": float(row.get("low") or 0),
                    "close": float(row.get("close") or 0),
                    "volume": int(row.get("volume") or 0),
                    "currency": row["currency"],
                    "provider": row.get("provider", provider),
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
                message=f"Ingested {len(df)} bars for {symbol}",
                latency_ms=0,
            )

            return {
                "success": True,
                "symbol": symbol,
                "rows_inserted": len(df),
                "provider": provider,
                "date_range": {
                    "start": str(df["ts"].min()),
                    "end": str(df["ts"].max()),
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
