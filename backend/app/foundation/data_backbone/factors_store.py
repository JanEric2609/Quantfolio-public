"""Factor loadings read/write API over the factor_loadings_daily hypertable."""

from datetime import datetime
import logging

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.foundation.data_backbone._session_guard import safe_execute

logger = logging.getLogger(__name__)


class FactorStore:
    """Read/write API for factor_loadings_daily hypertable."""

    def __init__(self, db: Session):
        self.db = db

    def write_factor_loadings(
        self,
        symbol: str,
        ts: datetime,
        factor_loadings: dict[str, float],
    ) -> bool:
        """Write factor loadings for a symbol on a date.

        Args:
            symbol: Ticker symbol.
            ts: Date.
            factor_loadings: Dict of {factor_name: loading_value}.

        Returns:
            True if successful.
        """
        try:
            with self.db.begin():
                for factor, loading in factor_loadings.items():
                    # Name the (symbol, factor, ts) unique index (migration 0075);
                    # a target-less DO NOTHING never fired (only (id, ts) was unique),
                    # so factor loadings duplicated on every re-run.
                    sql = text("""
                        INSERT INTO factor_loadings_daily (symbol, factor, ts, loading)
                        VALUES (:symbol, :factor, :ts, :loading)
                        ON CONFLICT (symbol, factor, ts) DO UPDATE SET
                            loading = EXCLUDED.loading
                    """)
                    self.db.execute(
                        sql,
                        {
                            "symbol": symbol,
                            "factor": factor,
                            "ts": ts,
                            "loading": float(loading),
                        },
                    )
            return True
        except Exception as e:
            logger.error(f"Error writing factor loadings for {symbol}: {e}")
            return False

    def get_factor_loadings(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame | None:
        """Fetch factor loadings for a symbol.

        Args:
            symbol: Ticker symbol.
            start: Start date (inclusive).
            end: End date (inclusive).

        Returns:
            DataFrame with columns (ts, factor, loading) or None if no data.
        """
        try:
            sql = """
                SELECT ts, factor, loading
                FROM factor_loadings_daily
                WHERE symbol = :symbol
            """

            params = {"symbol": symbol}

            if start:
                sql += " AND ts >= :start"
                params["start"] = start.isoformat()

            if end:
                sql += " AND ts <= :end"
                params["end"] = end.isoformat()

            sql += " ORDER BY ts ASC, factor ASC"

            result = safe_execute(self.db, text(sql), params)
            rows = result.fetchall()

            if not rows:
                return None

            df = pd.DataFrame(rows, columns=pd.Index(["ts", "factor", "loading"]))
            df["ts"] = pd.to_datetime(df["ts"])
            return df

        except Exception as e:
            logger.error(f"Error fetching factor loadings for {symbol}: {e}")
            return None

    def get_factor_loadings_pivot(
        self,
        symbol: str,
        ts: datetime,
    ) -> dict[str, float] | None:
        """Get factor loadings for a symbol on a specific date (pivot format).

        Returns:
            Dict of {factor_name: loading_value} or None if no data.
        """
        try:
            sql = text("""
                SELECT factor, loading
                FROM factor_loadings_daily
                WHERE symbol = :symbol AND ts = :ts
            """)

            result = safe_execute(self.db, sql, {"symbol": symbol, "ts": ts})
            rows = result.fetchall()

            if not rows:
                return None

            return {row[0]: row[1] for row in rows}

        except Exception as e:
            logger.error(f"Error fetching factor loadings pivot for {symbol}: {e}")
            return None

    def get_all_factors(self) -> list[str]:
        """Get list of all factors in the store."""
        try:
            sql = text("SELECT DISTINCT factor FROM factor_loadings_daily ORDER BY factor")
            result = safe_execute(self.db, sql)
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.error(f"Error listing factors: {e}")
            return []

    def get_symbols_for_factor(self, factor: str) -> list[str]:
        """Get all symbols that have loadings for a factor."""
        try:
            sql = text("""
                SELECT DISTINCT symbol FROM factor_loadings_daily
                WHERE factor = :factor
                ORDER BY symbol
            """)
            result = safe_execute(self.db, sql, {"factor": factor})
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.error(f"Error listing symbols for factor {factor}: {e}")
            return []

    def delete_factor_loadings(
        self,
        symbol: str,
        factor: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Delete factor loadings matching criteria.

        Args:
            symbol: Ticker symbol.
            factor: Optional factor name (if None, delete all factors for symbol).
            start: Start date (inclusive).
            end: End date (inclusive).

        Returns:
            Number of rows deleted.
        """
        try:
            sql = "DELETE FROM factor_loadings_daily WHERE symbol = :symbol"
            params = {"symbol": symbol}

            if factor:
                sql += " AND factor = :factor"
                params["factor"] = factor

            if start:
                sql += " AND ts >= :start"
                params["start"] = start.isoformat()

            if end:
                sql += " AND ts <= :end"
                params["end"] = end.isoformat()

            result = safe_execute(self.db, text(sql), params)
            self.db.commit()
            if isinstance(result, CursorResult):
                return result.rowcount
            return 0

        except Exception as e:
            logger.error(f"Error deleting factor loadings: {e}")
            return 0
