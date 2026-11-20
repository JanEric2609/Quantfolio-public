"""Regime snapshots read/write API over the regime_snapshots hypertable."""

from datetime import datetime, timezone
import json
import logging
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.foundation.data_backbone._session_guard import safe_execute

logger = logging.getLogger(__name__)


class RegimeStore:
    """Read/write API for regime_snapshots hypertable."""

    def __init__(self, db: Session):
        self.db = db

    def write_snapshot(
        self,
        ts: datetime,
        label: str,
        score: float,
        source: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Write a regime snapshot.

        Args:
            ts: Timestamp.
            label: Regime label (e.g., 'bull', 'bear', 'transition').
            score: Numeric score (0-1 or unbounded depending on source).
            source: Source of the regime determination (e.g., 'hmm', 'ml', 'manual').
            payload: Optional JSON payload with details.

        Returns:
            True if successful.
        """
        try:
            payload_json = json.dumps(payload or {})
            sql = text("""
                INSERT INTO regime_snapshots (ts, label, score, source, payload_json)
                VALUES (:ts, :label, :score, :source, :payload_json)
            """)
            safe_execute(
                self.db,
                sql,
                {
                    "ts": ts,
                    "label": label,
                    "score": float(score),
                    "source": source,
                    "payload_json": payload_json,
                },
            )
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"Error writing regime snapshot: {e}")
            return False

    def get_latest_snapshot(self, label: str | None = None, source: str | None = None) -> dict[str, Any] | None:
        """Get the most recent regime snapshot.

        Args:
            label: Optional label filter.
            source: Optional model filter ("jump", "hmm").

        Returns:
            Dict with ts, label, score, source, payload and age_hours
            (snapshot age in hours, float) or None if no data.
        """
        try:
            sql = """
                SELECT ts, label, score, source, payload_json
                FROM regime_snapshots
            """
            params = {}
            clauses = []
            if label:
                clauses.append("label = :label")
                params["label"] = label
            if source:
                clauses.append("source = :source")
                params["source"] = source
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)

            sql += " ORDER BY ts DESC LIMIT 1"

            result = safe_execute(self.db, text(sql), params)
            row = result.fetchone()

            if not row:
                return None

            ts = row[0]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0

            return {
                "ts": row[0],
                "label": row[1],
                "score": row[2],
                "source": row[3],
                "payload": json.loads(row[4]) if row[4] else {},
                "age_hours": age_hours,
            }

        except Exception as e:
            logger.error(f"Error fetching latest regime snapshot: {e}")
            return None

    def get_history(
        self,
        label: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame | None:
        """Fetch regime snapshot history.

        Args:
            label: Optional label filter.
            start: Start date (inclusive).
            end: End date (inclusive).

        Returns:
            DataFrame with columns (ts, label, score, source) or None.
        """
        try:
            sql = """
                SELECT ts, label, score, source
                FROM regime_snapshots
                WHERE 1=1
            """
            params = {}

            if label:
                sql += " AND label = :label"
                params["label"] = label

            if start:
                sql += " AND ts >= :start"
                params["start"] = start

            if end:
                sql += " AND ts <= :end"
                params["end"] = end

            sql += " ORDER BY ts ASC"

            result = self.db.execute(text(sql), params)
            rows = result.fetchall()

            if not rows:
                return None

            df = pd.DataFrame(rows, columns=pd.Index(["ts", "label", "score", "source"]))
            df["ts"] = pd.to_datetime(df["ts"])
            return df

        except Exception as e:
            logger.error(f"Error fetching regime history: {e}")
            return None

    def get_labels(self) -> list[str]:
        """Get all unique regime labels."""
        try:
            sql = text("""
                SELECT DISTINCT label
                FROM regime_snapshots
                ORDER BY label
            """)
            result = safe_execute(self.db, sql)
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.error(f"Error listing labels: {e}")
            return []

    def detect_regime_changes(
        self,
        label: str,
        start: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Detect regime changes for a label (score reversals).

        Args:
            label: Regime label.
            start: Optional start date.

        Returns:
            List of change events.
        """
        try:
            sql = """
                SELECT
                    ts,
                    label,
                    score,
                    LAG(score) OVER (ORDER BY ts) as prev_score
                FROM regime_snapshots
                WHERE label = :label
            """
            params = {"label": label}

            if start:
                sql += " AND ts >= :start"
                params["start"] = start.isoformat()

            sql += " ORDER BY ts ASC"

            result = self.db.execute(text(sql), params)
            rows = result.fetchall()

            changes = []
            for ts, lbl, score, prev_score in rows:
                if prev_score is not None:
                    # Detect sign changes (bull -> bear or vice versa)
                    if (prev_score >= 0 and score < 0) or (prev_score < 0 and score >= 0):
                        changes.append({
                            "ts": ts,
                            "label": lbl,
                            "prev_score": prev_score,
                            "new_score": score,
                            "direction": "positive" if score >= 0 else "negative",
                        })

            return changes

        except Exception as e:
            logger.error(f"Error detecting regime changes: {e}")
            return []

    def delete_snapshots(
        self,
        label: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Delete regime snapshots matching criteria.

        Args:
            label: Optional label filter.
            start: Start date (inclusive).
            end: End date (inclusive).

        Returns:
            Number of rows deleted.
        """
        try:
            sql = "DELETE FROM regime_snapshots WHERE 1=1"
            params = {}

            if label:
                sql += " AND label = :label"
                params["label"] = label

            if start:
                sql += " AND ts >= :start"
                params["start"] = start

            if end:
                sql += " AND ts <= :end"
                params["end"] = end

            result = self.db.execute(text(sql), params)
            self.db.commit()
            if isinstance(result, CursorResult):
                return result.rowcount
            return 0

        except Exception as e:
            logger.error(f"Error deleting regime snapshots: {e}")
            return 0
