"""The model panel: the JKP universe as one month-sorted Parquet file, read in chunks.

The universe is ``factor_premia``'s: one row per ``(gvkey, eom)`` (the latest
extract wins), in the region's countries, without micro and nano caps, with a
positive ``me``, a next-month return ``ret_exc_lead1m``, and at least
``MIN_STOCKS_PER_COUNTRY_MONTH`` such stocks in its country that month.

DuckDB writes that universe once, sorted by month, next to the panel
(``derived/``), so each later chunk read touches only its own row groups. The
file is named after a fingerprint of the source partitions and is rebuilt when
they change. Ranking happens per chunk in pandas: every characteristic and the
label become centred ranks within country and month, in (-0.5, 0.5), and a
missing value becomes 0 (the middle).
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

from app.foundation.data_engineering.panel_sql import (
    WRDS_FACTOR_CHARACTERISTICS_DIR,
    panel_partition_paths,
    query_panel,
)
from app.lab.factor_premia.strategies import EXCLUDED_SIZE_GROUPS, MIN_STOCKS_PER_COUNTRY_MONTH, REGIONS
from app.lab.pooled_model.spec import FEATURES

DERIVED_DIR = "derived"
# Bump when the universe, the file layout or the row order changes, so old
# files are rebuilt. Rows are in (eom, excntry, gvkey) order: the boosted
# trees' fixed-seed sample picks by row position, so any looser order would
# let a rebuild change the model.
PANEL_VERSION = 3
_KEYS = ["eom", "excntry"]


@dataclass(frozen=True)
class Chunk:
    """Rows of consecutive months, ranked and ready for the models."""

    month: np.ndarray  # index into the study's month list, per row
    eom: np.ndarray
    gvkey: np.ndarray
    excntry: np.ndarray
    size_grp: np.ndarray
    me: np.ndarray
    r: np.ndarray  # raw next-month excess return
    y: np.ndarray  # centred rank of r within country and month (the label)
    x: np.ndarray  # float32, rows x FEATURES, centred ranks, missing = 0


def _fingerprint(paths: list[str], region: str) -> str:
    stats = [(p, os.stat(p).st_size, os.stat(p).st_mtime_ns) for p in paths]
    blob = json.dumps([PANEL_VERSION, region, list(FEATURES), stats])
    return hashlib.sha1(blob.encode(), usedforsecurity=False).hexdigest()[:12]


def _available_columns(paths: list[str]) -> set[str]:
    frame = query_panel("DESCRIBE SELECT * FROM read_parquet(?, union_by_name = true)", [paths])
    return set(frame["column_name"])


def _universe_sql(available: set[str]) -> str:
    # Formatted only from the FEATURES constant and the extract's own column
    # names (a column the extract lacks is selected as NULL); runtime values
    # are bound parameters.
    features = ",\n        ".join(
        f"CAST({f} AS FLOAT) AS {f}" if f in available else f"CAST(NULL AS FLOAT) AS {f}"
        for f in FEATURES
    )
    return f"""
WITH scan AS (
    SELECT * FROM read_parquet(?, union_by_name = true)
    WHERE excntry IN (SELECT unnest(?))
),
latest AS (
    SELECT * FROM scan
    QUALIFY row_number() OVER (PARTITION BY gvkey, eom ORDER BY ingested_at DESC) = 1
),
universe AS (
    SELECT * FROM latest
    WHERE me > 0 AND ret_exc_lead1m IS NOT NULL AND isfinite(ret_exc_lead1m) AND ret_exc_lead1m > -1
      AND coalesce(size_grp, '') NOT IN (SELECT unnest(?))
),
counted AS (
    SELECT *, count(*) OVER (PARTITION BY eom, excntry) AS n FROM universe
)
SELECT CAST(eom AS DATE) AS eom, CAST(gvkey AS VARCHAR) AS gvkey, excntry, size_grp,
       CAST(me AS DOUBLE) AS me,
       CAST(ret_exc_lead1m AS DOUBLE) AS r,
        {features}
FROM counted WHERE n >= ?
ORDER BY eom, excntry, gvkey
"""  # noqa: S608


def model_panel_path(panel_dir: Path, region: str) -> Path | None:
    """The region's month-sorted universe file, built if missing or stale.

    Returns None when the characteristics panel has no partitions.
    """
    paths = panel_partition_paths(panel_dir, WRDS_FACTOR_CHARACTERISTICS_DIR)
    if not paths:
        return None
    out_dir = panel_dir / DERIVED_DIR
    out = out_dir / f"pooled_model_{region}_{_fingerprint(paths, region)}.parquet"
    if out.exists():
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    # COPY's target cannot be a bound parameter; the path is built here from
    # the panel dir and a hex digest, and quoted.
    target = str(tmp).replace("'", "''")
    query_panel(
        f"COPY ({_universe_sql(_available_columns(paths))}) TO '{target}' "  # noqa: S608
        "(FORMAT parquet, ROW_GROUP_SIZE 50000)",
        [paths, list(REGIONS[region]), list(EXCLUDED_SIZE_GROUPS), MIN_STOCKS_PER_COUNTRY_MONTH],
    )
    tmp.replace(out)
    # Only fingerprinted universe files: the study's scores file shares the prefix.
    for stale in out_dir.glob(f"pooled_model_{region}_{'?' * len(out.stem.rsplit('_', 1)[1])}.parquet"):
        if stale != out:
            stale.unlink(missing_ok=True)
    return out


def panel_months(path: Path) -> list[pd.Timestamp]:
    """Every month end in the file, ascending."""
    frame = query_panel("SELECT DISTINCT eom FROM read_parquet(?) ORDER BY eom", [str(path)])
    return [cast(pd.Timestamp, pd.Timestamp(m)) for m in frame["eom"]]


def panel_rows(path: Path) -> int:
    return int(query_panel("SELECT count(*) AS n FROM read_parquet(?)", [str(path)])["n"].iloc[0])


def centred_ranks(frame: pd.DataFrame, columns: list[str]) -> np.ndarray:
    """Average ranks within country and month, scaled to (-0.5, 0.5); NaN -> 0."""
    grouped = frame.groupby(_KEYS, sort=False)[columns]
    ranks = grouped.rank(method="average")
    counts = grouped.transform("count")
    return ((ranks - 0.5) / counts - 0.5).fillna(0.0).to_numpy(dtype=np.float32)


def iter_chunks(path: Path, months: list[pd.Timestamp], months_per_chunk: int = 12) -> Iterator[Chunk]:
    """Yield the file in blocks of whole months, in month order."""
    index = {m: i for i, m in enumerate(months)}
    for start in range(0, len(months), months_per_chunk):
        block = months[start:start + months_per_chunk]
        frame = query_panel(
            "SELECT * FROM read_parquet(?) WHERE eom BETWEEN ? AND ? ORDER BY eom, excntry, gvkey",
            [str(path), block[0].date(), block[-1].date()],
        )
        if frame.empty:
            continue
        eom = pd.to_datetime(frame["eom"])
        yield Chunk(
            month=eom.map(index).to_numpy(dtype=np.int32),
            eom=eom.to_numpy(),
            gvkey=frame["gvkey"].to_numpy(dtype=object),
            excntry=frame["excntry"].to_numpy(dtype=object),
            size_grp=frame["size_grp"].to_numpy(dtype=object),
            me=frame["me"].to_numpy(dtype=np.float64),
            r=frame["r"].to_numpy(dtype=np.float64),
            y=centred_ranks(frame, ["r"])[:, 0],
            x=centred_ranks(frame, list(FEATURES)),
        )
