"""AlphaCrafter data ingestion: make sure the miner's universe has bars.

The miner's panel (``panel.build_panel``) reads ``bar_prices`` only. This
module fills that table for the universe through the shared
``DataIngester.ingest_bar_prices``, which already owns provider order,
rate limits and provider-health recording. It replaces a per-provider
cascade whose only step raised ``NotImplementedError``, so nothing was ever
downloaded: the miner saw whatever other jobs had happened to store. That
was enough for four ETFs; it is not for the 150-name cross-section
(alphacrafter/universe.py).

Never fails silently and never raises: every symbol ends up counted in
``price_symbols`` or listed in ``failed_symbols``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, cast

import pandas as pd
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Stored bars that start within this many days of the window start count as
# covering it (weekends, holidays, a listing's first trading day).
_START_SLACK_DAYS = 10
# ... and that end within this many days of the window end are current.
_END_SLACK_DAYS = 4


@dataclass
class DataAvailability:
    """Tracks data availability across symbols and providers."""

    price_symbols: int = 0
    fundamental_symbols: int = 0
    news_symbols: int = 0
    failed_symbols: list[str] = field(default_factory=list)
    provider_used: dict[str, str] = field(default_factory=dict)


def _utc(value: Any) -> datetime:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize(timezone.utc) if ts.tzinfo is None else ts.tz_convert(timezone.utc)
    return cast(datetime, ts.to_pydatetime())


class AlphaCrafterDataIngestion:
    """Bring ``bar_prices`` up to date for a universe before the miner runs."""

    async def ensure_data(
        self,
        db: Session,
        universe: list[str],
        start_date: datetime,
        end_date: datetime,
    ) -> DataAvailability:
        """Ingest what is missing for each symbol over [start_date, end_date].

        Returns:
            DataAvailability with counts and, per symbol, ``"stored"`` or the
            provider that supplied the new bars.
        """
        availability = DataAvailability()

        for symbol in universe:
            try:
                result = await self._download_symbol(db, symbol, start_date, end_date)
                if result.get("success"):
                    availability.price_symbols += 1
                    provider = result.get("provider")
                    if provider:
                        availability.provider_used[symbol] = provider
                else:
                    availability.failed_symbols.append(symbol)
                    logger.warning(
                        f"Failed to download data for {symbol}: {result.get('error')}"
                    )
            except Exception as e:
                availability.failed_symbols.append(symbol)
                logger.error(f"Unexpected error downloading {symbol}: {e}")

        return availability

    async def _download_symbol(
        self,
        db: Session,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
    ) -> dict[str, Any]:
        """Use stored bars when they cover the window; otherwise ingest the gap.

        A series that covers the start but ends early is caught up from its
        last bar; one that starts late (or does not exist) is ingested over
        the whole window. A listing younger than the window is re-requested
        each run, which costs one provider call.
        """
        from app.foundation.data_backbone.bars import BarStore  # noqa: PLC0415
        from app.foundation.data_backbone.ingest import DataIngester  # noqa: PLC0415

        start, end = _utc(start_date), _utc(end_date)
        bars = BarStore(db).get_bars(symbol=symbol, start=start, end=end)
        fetch_from = start
        if bars is not None and len(bars) > 0:
            first, last = _utc(bars["ts"].min()), _utc(bars["ts"].max())
            starts_in_time = first <= start + timedelta(days=_START_SLACK_DAYS)
            if starts_in_time and last >= end - timedelta(days=_END_SLACK_DAYS):
                return {"success": True, "symbol": symbol, "provider": "stored"}
            if starts_in_time:
                fetch_from = last - timedelta(days=1)

        result = DataIngester(db).ingest_bar_prices(
            symbol, start_date=fetch_from.date().isoformat(), end_date=end.date().isoformat(),
        )
        if result.get("success"):
            return {"success": True, "symbol": symbol, "provider": str(result.get("provider") or "ingested")}
        return {"success": False, "symbol": symbol, "error": result.get("message") or "ingest failed"}
