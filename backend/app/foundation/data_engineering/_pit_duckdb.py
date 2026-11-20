"""Predicate-pushdown Parquet reads for the three per-symbol PIT joins in
:mod:`app.foundation.data_engineering.pit_panel_joins` (WRDS factor
characteristics, IBES estimates, insider trading).

Each function here returns exactly what the matching loader's ``read_*``
function (``read_wrds_factor_characteristics`` / ``read_ibes_estimates`` /
``read_insider_trading``) would return, restricted to the rows the caller can
possibly match -- but the restriction happens inside DuckDB's Parquet scan,
so no full-dataset pandas frame is ever materialised. That bounds the peak
memory of one join call by the symbol's slice, not the dataset's size, which
is the risk ADR 0016 flagged and neither #264 (single-scan read) nor #265
(byte-bounded cache) closed. See
``docs/archive/plans/2026-09-22-pit-join-duckdb-migration.md`` ("Track A").

**Dedup-key-group filtering, not match-column filtering.** The loaders
dedup ("later extract wins") across the whole panel *before* anything is
filtered. For WRDS the filter column (``gvkey``) is part of the dedup key, so
pushing the filter down is trivially equivalent. For IBES and insider trading
it is not: callers match on ``oftic`` / ``ticker``, but dedup keys on IBES
``ticker`` / ``issuer_cik``. A later extract that changed a row's ``oftic``
would, under a naive ``WHERE oftic = ?``, leave the superseded row visible.
So those two queries select every row whose dedup key *ever* carried the
requested symbol (a subquery on the match column), dedup that superset in
pandas exactly like the loader, and leave the final match-column filter to
the caller -- same result as filtering the full deduped panel.

Dtype parity with ``pd.read_parquet``: results are fetched as an Arrow table
and converted with ``pyarrow.Table.to_pandas()`` -- the same conversion
``_parquet_combine.read_parquet_partitions`` uses -- rather than DuckDB's own
``.df()``, whose nullable-int / timestamp mapping differs.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import cast

import duckdb
import numpy as np
import pandas as pd

from app.foundation.data_engineering._parquet_combine import unified_schema
from app.foundation.data_engineering.ccm_link_schema import CCM_LINK_COLUMNS
from app.foundation.data_engineering.crsp_names_schema import CRSP_NAMES_COLUMNS
from app.foundation.data_engineering.ibes_estimates_schema import IBES_ESTIMATES_COLUMNS
from app.foundation.data_engineering.insider_trading_schema import (
    INSIDER_TRADING_COLUMNS,
    OPEN_MARKET_TRANSACTION_CODES,
)
from app.foundation.data_engineering.security_identifiers_schema import SECURITY_IDENTIFIERS_COLUMNS
from app.foundation.data_engineering.wrds_factors_schema import FACTOR_CHARACTERISTICS_COLUMNS

_DEFAULT_MEMORY_LIMIT = "2GB"
_DEFAULT_THREADS = "2"

# Same dedup keys as the loaders' read_* functions -- keep in sync.
_WRDS_DEDUP_KEY = ["gvkey", "eom"]
_IBES_DEDUP_KEY = ["ticker", "statpers", "fpedats", "fpi", "measure"]
_INSIDER_DEDUP_KEY = ["cik", "issuer_cik", "transaction_date", "transaction_code", "shares", "price_per_share"]
_CCM_DEDUP_KEY = ["gvkey", "permno", "linkdt", "linktype", "linkprim"]


def _match_key_sql(column: str) -> str:
    """SQL twin of :func:`symbol_match_key` / :func:`symbol_match_keys`."""
    return f"regexp_replace(upper(coalesce({column}, '')), '[^A-Z0-9]', '', 'g')"

_connection_lock = threading.Lock()
_connection_singleton: duckdb.DuckDBPyConnection | None = None


def _duckdb_config() -> dict[str, str | bool | int | float | list[str]]:
    """Resolve DuckDB resource settings: env var first, then DB setting, then default.

    Same resolution order as :func:`app.foundation.data_engineering.paths.get_panel_dir`.
    DuckDB's own ``memory_limit`` default is 80% of *host* RAM, which is wrong
    for a shared container, so a limit is always set. ``temp_directory`` is
    only set when configured (DuckDB's default otherwise).
    """
    keys = {
        "memory_limit": ("QUANTFOLIO_DUCKDB_MEMORY_LIMIT", "duckdb_memory_limit", _DEFAULT_MEMORY_LIMIT),
        "threads": ("QUANTFOLIO_DUCKDB_THREADS", "duckdb_threads", _DEFAULT_THREADS),
        "temp_directory": ("QUANTFOLIO_DUCKDB_TMPDIR", "duckdb_temp_directory", ""),
    }
    settings: dict = {}
    if not all(os.environ.get(env) for env, _, _ in keys.values()):
        try:
            from app.foundation.core.db import SessionLocal
            from app.foundation.settings import get_public_settings

            with SessionLocal() as db:
                settings = get_public_settings(db)
        except Exception:
            settings = {}

    config: dict[str, str | bool | int | float | list[str]] = {}
    for name, (env, setting_key, default) in keys.items():
        value = os.environ.get(env) or settings.get(setting_key) or default
        if value:
            config[name] = str(value)
    return config


def _connection() -> duckdb.DuckDBPyConnection:
    """Process-lifetime in-memory DuckDB connection, created on first use.

    Holds no views or tables: every query passes its Parquet file list as a
    parameter, so nothing is bound to a particular panel dir and nothing needs
    invalidating when it changes (tests repoint it per test).
    """
    global _connection_singleton
    with _connection_lock:
        if _connection_singleton is None:
            _connection_singleton = duckdb.connect(":memory:", config=_duckdb_config())
        return _connection_singleton


def reset_connection() -> None:
    """Close and drop the singleton connection (tests / config reload)."""
    global _connection_singleton
    with _connection_lock:
        if _connection_singleton is not None:
            _connection_singleton.close()
        _connection_singleton = None


def _partition_paths(panel_dir: Path, subdir: str) -> list[str]:
    # Mirrors the loaders' ``sorted(out_dir.glob("*.parquet"))``: an explicit
    # sorted file list keeps row order identical to their pyarrow scan and
    # sidesteps DuckDB raising on a glob that matches nothing.
    out_dir = panel_dir / subdir
    return [str(p) for p in sorted(out_dir.glob("*.parquet"))] if out_dir.exists() else []


def _query(sql: str, params: list | dict, paths: list[str]) -> pd.DataFrame:
    # A cursor per call: DuckDBPyConnection is not safe to share across
    # threads, and APScheduler / Starlette both call in from thread pools.
    with _connection().cursor() as cursor:
        table = cursor.execute(sql, params).to_arrow_table()
    # DuckDB's Arrow output drops the Parquet file's pandas metadata (so
    # string columns come back ``object`` rather than ``string``) and may
    # widen/narrow types. Casting back to the partitions' unified schema --
    # the one read_parquet_partitions scans with -- restores both, so the
    # pandas conversion matches the loaders' exactly, including columns only
    # newer partitions carry.
    schema = unified_schema(paths)
    table = table.select(schema.names).cast(schema)
    return table.to_pandas()


def _dedup_latest(frame: pd.DataFrame, key: list[str]) -> pd.DataFrame:
    frame = frame.sort_values("ingested_at", kind="stable")
    return cast(pd.DataFrame, frame.drop_duplicates(subset=key, keep="last"))


def wrds_factor_characteristics_for_gvkeys(panel_dir: Path, gvkeys: list[str]) -> pd.DataFrame:
    """``read_wrds_factor_characteristics()`` restricted to *gvkeys*."""
    paths = _partition_paths(panel_dir, "wrds_factor_characteristics_pit")
    if not paths or not gvkeys:
        return pd.DataFrame(columns=pd.Index(FACTOR_CHARACTERISTICS_COLUMNS))

    frame = _query(
        "SELECT * FROM read_parquet(?, union_by_name = true) WHERE gvkey IN (SELECT unnest(?))",
        [paths, list(gvkeys)],
        paths,
    )
    frame = _dedup_latest(frame, _WRDS_DEDUP_KEY)
    return frame.sort_values(["gvkey", "eom"]).reset_index(drop=True)


def ibes_estimates_for_oftic(panel_dir: Path, oftic: str, fpi: str, measure: str) -> pd.DataFrame:
    """``read_ibes_estimates(fpi=fpi, measure=measure)`` restricted to every
    IBES ``ticker`` that has ever carried *oftic* under :func:`symbol_match_key`
    (so ``BRK-B`` finds ``BRK.B``).

    The caller still applies its own ``oftic`` match -- see module docstring.
    """
    paths = _partition_paths(panel_dir, "ibes_estimates_pit")
    if not paths:
        return pd.DataFrame(columns=pd.Index(IBES_ESTIMATES_COLUMNS))

    frame = _query(
        # Interpolated: _match_key_sql over a literal column name only; values bind.
        "SELECT * FROM read_parquet($paths, union_by_name = true) "  # noqa: S608
        "WHERE fpi = $fpi AND measure = $measure AND ticker IN ("
        "  SELECT DISTINCT ticker FROM read_parquet($paths, union_by_name = true)"
        f"  WHERE {_match_key_sql('oftic')} = $oftic"
        ")",
        {"paths": paths, "fpi": fpi, "measure": measure.upper(), "oftic": symbol_match_key(oftic)},
        paths,
    )
    frame = _dedup_latest(frame, _IBES_DEDUP_KEY)
    return frame.sort_values(["ticker", "statpers"]).reset_index(drop=True)


def insider_trading_for_ticker(panel_dir: Path, ticker: str) -> pd.DataFrame:
    """``read_insider_trading(open_market_only=True)`` restricted to every
    ``issuer_cik`` that has ever filed under *ticker* under
    :func:`symbol_match_key` (so ``BRK-B`` finds ``BRK.B``).

    The caller still applies its own ``ticker`` match -- see module docstring.
    """
    paths = _partition_paths(panel_dir, "insider_trading_pit")
    if not paths:
        return pd.DataFrame(columns=pd.Index(INSIDER_TRADING_COLUMNS))

    frame = _query(
        # Interpolated: _match_key_sql over a literal column name only; values bind.
        "SELECT * FROM read_parquet($paths, union_by_name = true) "  # noqa: S608
        f"WHERE transaction_code IN (SELECT unnest($codes)) AND ({_match_key_sql('ticker')} = $ticker "
        "OR issuer_cik IN ("
        "  SELECT DISTINCT issuer_cik FROM read_parquet($paths, union_by_name = true)"
        f"  WHERE {_match_key_sql('ticker')} = $ticker"
        "))",
        {"paths": paths, "codes": list(OPEN_MARKET_TRANSACTION_CODES), "ticker": symbol_match_key(ticker)},
        paths,
    )
    frame = _dedup_latest(frame, _INSIDER_DEDUP_KEY)
    return frame.sort_values(["cik", "filing_date"]).reset_index(drop=True)


def insider_trading_coverage(panel_dir: Path) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """First and last ``filing_date`` in the whole insider-trading extract.

    The extract is a one-off WRDS/SEC pull, not a feed: a ticker with no
    filings in a window is only evidence of "no insider trading" while the
    window lies inside what the extract covers. None when nothing is loaded.
    """
    paths = _partition_paths(panel_dir, "insider_trading_pit")
    if not paths:
        return None
    with _connection().cursor() as cursor:
        row = cursor.execute(
            "SELECT min(filing_date), max(filing_date) FROM read_parquet($paths, union_by_name = true)",
            {"paths": paths},
        ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    first, last = pd.Timestamp(row[0]), pd.Timestamp(row[1])
    if not isinstance(first, pd.Timestamp) or not isinstance(last, pd.Timestamp):
        return None  # NaT
    return first, last


def panel_newest(panel_dir: Path, subdir: str, column: str) -> tuple[pd.Timestamp | None, int]:
    """Newest *column* value and row count of a panel; ``(None, 0)`` when empty.

    *column* comes from the caller's fixed list, never from a request.
    """
    paths = _partition_paths(panel_dir, subdir)
    if not paths:
        return None, 0
    if not column.replace("_", "").isalnum():
        raise ValueError(f"bad column name {column!r}")
    with _connection().cursor() as cursor:
        row = cursor.execute(
            f"SELECT max({column}), count(*) FROM read_parquet($paths, union_by_name = true)",  # noqa: S608
            {"paths": paths},
        ).fetchone()
    if row is None:
        return None, 0
    newest = pd.Timestamp(row[0]) if row[0] is not None else None
    return (newest if isinstance(newest, pd.Timestamp) else None), int(row[1] or 0)


def security_identifiers_for_isin(panel_dir: Path, isin: str) -> pd.DataFrame:
    """``read_security_identifiers()`` restricted to *isin*.

    ``isin`` is part of the loader's dedup key, so filtering inside the scan
    is equivalent to filtering the deduped full panel -- which is ~16.7M rows
    for one year of Compustat daily history and must never be materialised
    for a single-symbol lookup.
    """
    paths = _partition_paths(panel_dir, "security_identifiers_pit")
    if not paths:
        return pd.DataFrame(columns=pd.Index(SECURITY_IDENTIFIERS_COLUMNS))

    frame = _query(
        "SELECT * FROM read_parquet(?, union_by_name = true) WHERE isin = ?",
        [paths, isin],
        paths,
    )
    frame["as_of_date"] = frame["as_of_date"].astype("datetime64[ns]")
    frame["gvkey"] = frame["gvkey"].astype("string")
    frame = cast(pd.DataFrame, frame.drop_duplicates(subset=["gvkey", "isin", "fic", "as_of_date"]))
    return frame.sort_values(["gvkey", "as_of_date"]).reset_index(drop=True)


def symbol_match_key(symbol: str) -> str:
    """Upper-case alphanumerics only: ``BRK-B`` / ``BRK.B`` / ``BRKB`` all match."""
    return "".join(ch for ch in symbol.upper() if ch.isalnum())


def symbol_match_keys(symbols: pd.Series) -> pd.Series:
    """Vectorised :func:`symbol_match_key` for a caller's post-query match."""
    return symbols.astype("string").str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)


def crsp_names_for_symbol(panel_dir: Path, symbol: str) -> pd.DataFrame:
    """``read_crsp_names()`` rows whose ticker or trading symbol matches *symbol*
    under :func:`symbol_match_key`. Both match columns are in the dedup key, so
    the pushed-down filter is equivalent to filtering the deduped panel."""
    paths = _partition_paths(panel_dir, "crsp_names_pit")
    key = symbol_match_key(symbol)
    if not paths or not key:
        return pd.DataFrame(columns=pd.Index(CRSP_NAMES_COLUMNS))

    frame = _query(
        # Interpolated: _match_key_sql over literal column names only; values bind.
        "SELECT * FROM read_parquet($paths, union_by_name = true) "  # noqa: S608
        f"WHERE {_match_key_sql('ticker')} = $key OR {_match_key_sql('trading_symbol')} = $key",
        {"paths": paths, "key": key},
        paths,
    )
    frame = _dedup_latest(frame, ["permno", "namedt", "ticker", "trading_symbol"])
    return frame.sort_values(["permno", "namedt"]).reset_index(drop=True)


def insider_trailing_window_signal(
    transactions: pd.DataFrame,
    dates: pd.DatetimeIndex,
    cluster_window_days: int,
    flow_window_days: int,
) -> tuple[list[int], list[float]]:
    """Trailing-window insider aggregates per requested date, as one range join.

    *transactions* is the already-filtered opportunistic frame from
    ``pit_insider_signal_for_symbol`` (``filing_date``, ``transaction_code``,
    ``cik``, ``shares``, ``price_per_share``); *dates* is tz-naive. For each
    date ``d`` (in order, duplicates and ``NaT`` included) returns:

    - distinct ``cik`` with a ``P`` filing in ``(d - cluster_window_days, d]``
    - signed dollar sum (buys minus sells) over ``(d - flow_window_days, d]``;
      ``0.0`` when the window is empty, ``NaN`` when any row in it has a
      missing ``shares``/``price_per_share`` -- the numpy loop's semantics.

    ADR 0016 Track B: a bounded-range join (``BETWEEN``-style + ``GROUP BY``),
    not an ``ASOF JOIN``, which only finds the single latest row. The cluster
    window is nested inside the flow window, so one join on the wider window
    feeds both aggregates.
    """
    if cluster_window_days > flow_window_days:
        raise ValueError("cluster window must not exceed the flow window")

    is_buy = (transactions["transaction_code"] == "P").to_numpy(dtype=bool)
    is_sell = (transactions["transaction_code"] == "S").to_numpy(dtype=bool)
    signed = (
        transactions["shares"].to_numpy(dtype="float64")
        * transactions["price_per_share"].to_numpy(dtype="float64")
        * (is_buy.astype("float64") - is_sell.astype("float64"))
    )
    nan_dollars = np.isnan(signed)
    tx = pd.DataFrame(
        {
            "filing_date": transactions["filing_date"].to_numpy(dtype="datetime64[ns]"),
            "is_buy": is_buy,
            "cik": transactions["cik"].astype("string"),
            # NaN is carried as a flag, not a value: DuckDB's pandas scan may
            # turn float NaN into NULL, which sum() would silently skip.
            "signed_dollars": np.where(nan_dollars, 0.0, signed),
            "nan_dollars": nan_dollars,
        }
    ).reset_index(drop=True)
    windows = pd.DataFrame(
        {
            "i": np.arange(len(dates), dtype="int64"),
            "d": dates.to_numpy(dtype="datetime64[ns]"),
            "cluster_start": (dates - pd.Timedelta(days=cluster_window_days)).to_numpy(dtype="datetime64[ns]"),
            "flow_start": (dates - pd.Timedelta(days=flow_window_days)).to_numpy(dtype="datetime64[ns]"),
        }
    )

    with _connection().cursor() as cursor:
        cursor.register("tx", tx)
        cursor.register("windows", windows)
        result = cursor.execute(
            "SELECT w.i,"
            "  count(DISTINCT t.cik) FILTER (WHERE t.is_buy AND t.filing_date > w.cluster_start) AS cluster,"
            "  coalesce(bool_or(t.nan_dollars), false) AS any_nan,"
            # Kahan summation: order-independent to the last bits, so
            # DuckDB's parallel aggregation can't make the value jitter.
            "  coalesce(fsum(t.signed_dollars), 0.0) AS flow "
            "FROM windows w LEFT JOIN tx t"
            "  ON t.filing_date > w.flow_start AND t.filing_date <= w.d "
            "GROUP BY w.i ORDER BY w.i"
        ).fetchnumpy()

    cluster = [int(v) for v in result["cluster"]]
    flow = [float("nan") if nan else float(v) for nan, v in zip(result["any_nan"], result["flow"], strict=True)]
    return cluster, flow


def _ccm_links(panel_dir: Path, where: str, params: dict) -> pd.DataFrame:
    paths = _partition_paths(panel_dir, "ccm_link_pit")
    if not paths:
        return pd.DataFrame(columns=pd.Index(CCM_LINK_COLUMNS))
    # ``where`` is one of the module's own constant fragments (callers below); values bind.
    frame = _query(f"SELECT * FROM read_parquet($paths, union_by_name = true) WHERE {where}", {"paths": paths, **params}, paths)  # noqa: S608
    frame = _dedup_latest(frame, _CCM_DEDUP_KEY)
    return frame.sort_values(["gvkey", "permno", "linkdt"]).reset_index(drop=True)


def ccm_links_for_permnos(panel_dir: Path, permnos: list[int]) -> pd.DataFrame:
    """``read_ccm_link()`` restricted to *permnos*. ``permno`` is in the dedup
    key, so the pushed-down filter is equivalent to filtering the deduped panel.
    Callers apply ``research_quality_only`` / ``as_of`` themselves."""
    if not permnos:
        return pd.DataFrame(columns=pd.Index(CCM_LINK_COLUMNS))
    return _ccm_links(panel_dir, "permno IN (SELECT unnest($permnos))", {"permnos": [int(p) for p in permnos]})


def ccm_links_for_gvkey(panel_dir: Path, gvkey: str) -> pd.DataFrame:
    """``read_ccm_link()`` restricted to *gvkey* (also in the dedup key)."""
    return _ccm_links(panel_dir, "gvkey = $gvkey", {"gvkey": str(gvkey)})
