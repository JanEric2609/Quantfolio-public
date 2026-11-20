"""Bar prices read API over the bar_prices hypertable."""

from datetime import datetime
import logging
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.foundation.data_backbone._session_guard import safe_execute

logger = logging.getLogger(__name__)


class BarStore:
    """Read API for bar_prices hypertable with caching support."""

    def __init__(self, db: Session):
        self.db = db

    def get_bars(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        as_of: datetime | None = None,
    ) -> pd.DataFrame | None:
        """Fetch OHLCV data for a symbol.

        Args:
            symbol: Ticker symbol.
            start: Start date (inclusive).
            end: End date (inclusive).
            as_of: Snapshot date (ignore data after this date).

        Returns:
            DataFrame with columns (ts, open, high, low, close, volume, currency, provider)
            or None if no data found.
        """
        try:
            sql = """
                SELECT ts, open, high, low, close, volume, currency, provider
                FROM bar_prices
                WHERE symbol = :symbol
            """

            params: dict[str, Any] = {"symbol": symbol}

            if start:
                sql += " AND ts >= :start"
                params["start"] = start

            if end:
                sql += " AND ts <= :end"
                params["end"] = end

            if as_of:
                sql += " AND ts <= :as_of"
                params["as_of"] = as_of

            sql += " ORDER BY ts ASC"

            result = safe_execute(self.db, text(sql), params)
            rows = result.fetchall()

            if not rows:
                return None

            df = pd.DataFrame(
                rows,
                columns=pd.Index(["ts", "open", "high", "low", "close", "volume", "currency", "provider"]),
            )
            df["ts"] = pd.to_datetime(df["ts"])
            return df

        except Exception as e:
            logger.error(f"Error fetching bars for {symbol}: {e}")
            return None

    def get_coverage(self, symbol: str) -> dict[str, Any] | None:
        """Get data coverage info for a symbol.

        Returns:
            Dict with first_ts, last_ts, gaps_count, last_provider.
        """
        try:
            # Use a query that works with both PostgreSQL and SQLite
            sql = text("""
                SELECT
                    min(ts) as first_ts,
                    max(ts) as last_ts,
                    count(*) as bar_count
                FROM bar_prices
                WHERE symbol = :symbol
            """)

            result = safe_execute(self.db, sql, {"symbol": symbol})
            row = result.fetchone()

            if not row or row[0] is None:
                return None

            # Get the last provider separately
            last_provider_sql = text("""
                SELECT provider
                FROM bar_prices
                WHERE symbol = :symbol
                ORDER BY ts DESC
                LIMIT 1
            """)
            provider_result = safe_execute(self.db, last_provider_sql, {"symbol": symbol})
            provider_row = provider_result.fetchone()
            last_provider = provider_row[0] if provider_row else None

            return {
                "symbol": symbol,
                "first_ts": row[0],
                "last_ts": row[1],
                "bar_count": row[2],
                "last_provider": last_provider,
                "gaps_count": self._estimate_gaps(symbol, row[0], row[1]),
            }

        except Exception as e:
            logger.error(f"Error getting coverage for {symbol}: {e}")
            return None

    def _estimate_gaps(self, symbol: str, start: datetime, end: datetime) -> int:
        """Estimate number of gaps in bar data (business days with no data)."""
        try:
            sql = text("""
                SELECT count(*) as gap_count
                FROM (
                    SELECT generate_series(:start, :end, interval '1 day') as date
                ) dates
                WHERE EXTRACT(dow FROM date) NOT IN (0, 6)
                AND date NOT IN (
                    SELECT CAST(ts AS DATE) FROM bar_prices WHERE symbol = :symbol
                )
            """)

            result = safe_execute(self.db, sql, {"symbol": symbol, "start": start, "end": end})
            row = result.fetchone()
            return row[0] if row else 0

        except Exception as e:
            logger.debug(f"Error estimating gaps for {symbol}: {e}")
            return -1  # Unknown

    def get_latest_bar(self, symbol: str) -> dict[str, Any] | None:
        """Get the most recent bar for a symbol."""
        try:
            sql = text("""
                SELECT ts, open, high, low, close, volume, currency, provider
                FROM bar_prices
                WHERE symbol = :symbol
                ORDER BY ts DESC
                LIMIT 1
            """)

            result = safe_execute(self.db, sql, {"symbol": symbol})
            row = result.fetchone()

            if not row:
                return None

            return {
                "ts": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
                "currency": row[6],
                "provider": row[7],
            }

        except Exception as e:
            logger.error(f"Error fetching latest bar for {symbol}: {e}")
            return None

    def list_symbols_with_data(self) -> list[str]:
        """List all symbols that have bar data."""
        try:
            sql = text("SELECT DISTINCT symbol FROM bar_prices ORDER BY symbol")
            result = safe_execute(self.db, sql)
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.error(f"Error listing symbols: {e}")
            return []
