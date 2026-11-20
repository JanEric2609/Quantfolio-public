"""Read API over the PIT Parquet panel, for Phase 4's lab to consume.

Reads whatever ``panel_export.py``/``datastream_loader.py`` have written to
``paths.get_panel_dir()``. Consumed today by
``app.lab.quant_lab.signal_batch.build_price_panel``; still not wired into
any live advisor/discover/graduation pathway -- that repoint is a later ADR
0015 phase.

Both partitions are read together, so the same (symbol, as_of_date) can
arrive twice -- once from the live-cache projection, once from a manual
extract. ``read_panel`` resolves that here, by the explicit
``panel_schema.SOURCE_PRECEDENCE`` order, rather than leaving a duplicate row
for a downstream ``pivot_table`` to silently collapse with ``aggfunc="last"``
(whose winner depends on file read order, and which cannot warn).
"""
from __future__ import annotations

import logging
from typing import cast

import pandas as pd

from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)


def _dedupe_by_source_precedence(panel: pd.DataFrame) -> pd.DataFrame:
    """Keep one row per (symbol, as_of_date), highest-precedence source wins.

    Logs a warning naming the affected symbols whenever a row is actually
    dropped: a collision means two producers disagree about the same bar, and
    that is worth seeing (the internal projection and a Datastream extract can
    differ in quotation unit -- GBp vs GBP -- which is a 100x silent error).
    """
    from app.foundation.data_engineering.panel_schema import SOURCE_PRECEDENCE

    if panel.empty or not panel.duplicated(subset=["symbol", "as_of_date"]).any():
        return panel

    rank = {source: i for i, source in enumerate(SOURCE_PRECEDENCE)}
    # Unknown sources sort last rather than raising: validate_panel_frame
    # already rejects them at write time, so a stale file is the only way to
    # get here and dropping the whole read would be the worse failure.
    unknown = len(rank)
    ordered = panel.assign(_rank=[rank.get(str(source), unknown) for source in panel["source"]])
    ordered = cast(pd.DataFrame, ordered.sort_values(by=["symbol", "as_of_date", "_rank"], kind="stable"))
    kept = cast(pd.DataFrame, ordered.drop_duplicates(subset=["symbol", "as_of_date"], keep="first"))

    dropped = len(panel) - len(kept)
    if dropped:
        collided = sorted(
            panel.loc[panel.duplicated(subset=["symbol", "as_of_date"], keep=False), "symbol"].unique().tolist()
        )
        logger.warning(
            "read_panel: %d duplicate (symbol, date) row(s) across %d symbol(s) resolved by source precedence %s; "
            "affected symbols: %s",
            dropped,
            len(collided),
            SOURCE_PRECEDENCE,
            collided[:20],
        )
    return cast(pd.DataFrame, kept.drop(columns=["_rank"]))


def read_panel(symbols: list[str] | None = None, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Read the PIT panel, optionally filtered by symbol and date range.

    Reads every ``*.parquet`` file under both the ``internal/`` and
    ``datastream_pit/`` partitions (whichever exist), concatenates them, and
    resolves cross-partition duplicates by ``SOURCE_PRECEDENCE``. Returns an
    empty DataFrame (with the canonical columns) if nothing has been
    exported/loaded yet, rather than raising.
    """
    from app.foundation.data_engineering.panel_schema import PANEL_COLUMNS

    panel_dir = get_panel_dir()
    frames: list[pd.DataFrame] = []
    for sub in ("internal", "datastream_pit"):
        source_dir = panel_dir / sub
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.glob("*.parquet")):
            try:
                frames.append(pd.read_parquet(path))
            except Exception:
                logger.exception("Failed to read panel partition %s", path)

    if not frames:
        return pd.DataFrame(columns=pd.Index(PANEL_COLUMNS))

    panel = _dedupe_by_source_precedence(pd.concat(frames, ignore_index=True))

    if symbols is not None:
        panel = cast(pd.DataFrame, panel[panel["symbol"].isin(symbols)])
    if start is not None:
        panel = cast(pd.DataFrame, panel[panel["as_of_date"] >= pd.Timestamp(start)])
    if end is not None:
        panel = cast(pd.DataFrame, panel[panel["as_of_date"] <= pd.Timestamp(end)])

    return panel.sort_values(by=["symbol", "as_of_date"]).reset_index(drop=True)
