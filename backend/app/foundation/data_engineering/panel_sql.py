"""Aggregate SQL over the PIT Parquet panel, for research code.

The per-symbol joins in :mod:`_pit_duckdb` fetch one symbol's slice. Research
code (``app.lab.factor_premia``) needs the opposite: a query over a whole
country set that returns a small aggregate, such as one row per month and
portfolio. Running the aggregation inside DuckDB keeps a multi-gigabyte JKP
extract out of pandas, which matters in a worker that has been OOM-killed.

Uses the same process-wide connection, and so the same ``memory_limit`` and
``threads`` settings, as the PIT joins.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.foundation.data_engineering._pit_duckdb import _connection, _partition_paths

WRDS_FACTOR_CHARACTERISTICS_DIR = "wrds_factor_characteristics_pit"


def panel_partition_paths(panel_dir: Path, subdir: str) -> list[str]:
    """Sorted Parquet partition paths of one panel table (empty if none)."""
    return _partition_paths(panel_dir, subdir)


def query_panel(sql: str, params: list | dict) -> pd.DataFrame:
    """Run *sql* on the shared DuckDB connection and return a pandas frame.

    Pass partition paths as a parameter (``read_parquet(?)``) rather than
    formatting them into the SQL.
    """
    with _connection().cursor() as cursor:
        return cursor.execute(sql, params).to_arrow_table().to_pandas()
