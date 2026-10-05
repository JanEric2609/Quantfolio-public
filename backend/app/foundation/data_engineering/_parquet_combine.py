"""Shared helper for combining a directory's Parquet partitions into one frame.

Reads every partition via ``pyarrow.dataset`` and converts to pandas exactly
once, instead of building a Python list of one :class:`pandas.DataFrame` per
partition and calling ``pd.concat`` -- that naive approach holds every
partition's frame resident at once *and* needs a further ~2x the combined
size at the moment of concatenation (a well-documented, unfixed ``pd.concat``
behaviour: the result is built as one new contiguous block before the inputs
can be freed). ``security_identifiers_loader.read_security_identifiers`` hit
exactly this in production (OOM-killed the scheduler daily); this helper
applies the same fix to its three siblings (``wrds_factors_loader``,
``ibes_estimates_loader``, ``insider_trading_loader``) without their extra
Arrow-backed string-dtype conversion, which needs schema-specific re-casting
of any join-key columns and is out of scope for this fix.

Deliberately omits a ``types_mapper``, so output dtypes match
``pd.read_parquet``'s own defaults -- a drop-in replacement for
``pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)``.

**Partitions may differ in columns.** A schema that gained columns (the JKP
characteristics panel did) leaves older partitions without them. Left to
itself, ``pyarrow.dataset`` takes the *first* file's schema and silently
drops columns only later files have, so :func:`unified_schema` is passed
explicitly: every column any partition has, missing values null.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pa_dataset
import pyarrow.parquet as pq


@lru_cache(maxsize=16)
def _unified_schema_cached(stamped: tuple[tuple[str, int, int], ...]) -> pa.Schema:
    schemas = [pq.read_schema(path) for path, _, _ in stamped]
    # The widest schema leads, so its column order and pandas metadata (which
    # restores nullable and string dtypes) cover every column.
    widest = max(schemas, key=len)
    return pa.unify_schemas([widest, *schemas]).with_metadata(widest.metadata)


def unified_schema(paths: list[Path] | list[str]) -> pa.Schema:
    """Union of the partitions' schemas, cached on each file's mtime and size."""
    stamped = []
    for path in paths:
        stat = os.stat(path)
        stamped.append((str(path), stat.st_mtime_ns, stat.st_size))
    return _unified_schema_cached(tuple(stamped))


def read_parquet_partitions(paths: list[Path]) -> pd.DataFrame:
    """Read and concatenate every path in *paths* as one Arrow dataset scan."""
    table = pa_dataset.dataset(
        [str(p) for p in paths], format="parquet", schema=unified_schema(paths)
    ).to_table()
    combined = table.to_pandas()
    del table
    return combined
