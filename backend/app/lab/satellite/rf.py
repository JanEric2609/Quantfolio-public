"""The US one-month T-bill rate, monthly, from Ken French's data library.

JKP returns are in excess of it. The satellite study adds it back because
German tax falls on the whole gain, not on the excess. The file is cached
next to the panel; history does not change, so a stale copy is used when
the download fails.
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

FF_FACTORS_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip"
CACHE_NAME = "ff_us_rf_monthly.parquet"
MAX_AGE_DAYS = 30


def parse_monthly_rf(text: str) -> pd.Series:
    """The ``RF`` column of the monthly block, as decimals indexed by month."""
    lines = text.splitlines()
    header = next(i for i, line in enumerate(lines) if line.replace(" ", "").startswith(",Mkt-RF"))
    columns = [c.strip() for c in lines[header].split(",")]
    rf_at = columns.index("RF")
    months, values = [], []
    for line in lines[header + 1:]:
        fields = [f.strip() for f in line.split(",")]
        if len(fields[0]) != 6 or not fields[0].isdigit():
            break  # the annual block follows the monthly one
        months.append(pd.Period(f"{fields[0][:4]}-{fields[0][4:]}", freq="M"))
        values.append(float(fields[rf_at]) / 100.0)
    return pd.Series(values, index=pd.PeriodIndex(months, freq="M"), name="rf")


def _download() -> pd.Series:
    import httpx

    resp = httpx.get(FF_FACTORS_URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        return parse_monthly_rf(zf.read(name).decode("utf-8", errors="replace"))


def load_risk_free(cache_dir: Path) -> pd.Series:
    """Monthly RF (decimal, ``PeriodIndex``); raises if neither download nor cache works."""
    cache = cache_dir / CACHE_NAME
    if cache.exists() and time.time() - cache.stat().st_mtime < MAX_AGE_DAYS * 86_400:
        return _read(cache)
    try:
        rf = _download()
    except Exception as exc:
        if cache.exists():
            logger.warning("satellite: RF download failed (%s); using the cached copy", exc)
            return _read(cache)
        raise RuntimeError(f"could not download the risk-free rate from {FF_FACTORS_URL}: {exc}") from exc
    cache.parent.mkdir(parents=True, exist_ok=True)
    rf.to_frame().assign(month=rf.index.astype(str)).to_parquet(cache, index=False)
    return rf


def _read(cache: Path) -> pd.Series:
    frame = pd.read_parquet(cache)
    return pd.Series(frame["rf"].to_numpy(), index=pd.PeriodIndex(frame["month"], freq="M"), name="rf")
