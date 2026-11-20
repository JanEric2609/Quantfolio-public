"""Deployment health probes for the data backbone.

These checks exist so that a missing-migration deployment fails *loudly*
instead of degrading opaquely: if the TimescaleDB hypertables the analytics
pipeline depends on are absent (migrations never applied to head), QuantLab,
regime gating, and price backfill silently no-op or abort with confusing
errors. The probes below surface that state directly.

``check_write_invariants`` exists because table *existence* alone missed the
ADR 0014 regression: ``2ce7981ac5c6_recreate_analytics`` recreated all four
tables (so they existed) but silently dropped their ``id`` default and unique
indexes, so every write failed with ``NotNullViolation``/
``InvalidColumnReference`` for months while ``check_critical_tables`` stayed
green. See ``alembic/versions/0106_restore_hypertable_writes.py``, which
re-applies exactly the invariants asserted here.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

# TimescaleDB hypertables created by migration 0015_hypertables.py (which
# depends on 0014_timescale_enable.py -> CREATE EXTENSION timescaledb). The
# analytics pipeline reads/writes these directly via raw SQL; if any are
# missing the run's shared session is poisoned and downstream work dies.
CRITICAL_TABLES: tuple[str, ...] = (
    "bar_prices",
    "regime_snapshots",
    "provider_health_history",
)

# Tables that must have an `id` server default (identity sequence) or every
# insert that omits `id` -- all of DataIngester/BarStore/FactorStore/
# RegimeStore -- raises NotNullViolation. Mirrors
# ``0106_restore_hypertable_writes.py``'s ``_ID_DEFAULT_TABLES``.
_ID_DEFAULT_TABLES: tuple[str, ...] = (
    "bar_prices",
    "factor_loadings_daily",
    "regime_snapshots",
    "provider_health_history",
)

# table -> unique index its ON CONFLICT upsert names. Only tables with a
# business-key upsert need one; regime_snapshots/provider_health_history are
# insert-only.
_UNIQUE_INDEXES: dict[str, str] = {
    "bar_prices": "uq_bar_prices_symbol_ts",
    "factor_loadings_daily": "uq_factor_loadings_symbol_factor_ts",
}


def check_critical_tables(db: Session) -> dict[str, Any]:
    """Return whether the analytics hypertables exist.

    ``missing`` lists absent tables; ``ok`` is False when any are missing.
    Inspection uses a fresh engine connection, so it never touches (or poisons)
    the caller's session/transaction.
    """
    try:
        existing = set(inspect(db.get_bind()).get_table_names())
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "missing": list(CRITICAL_TABLES), "error": repr(exc)}
    missing = [name for name in CRITICAL_TABLES if name not in existing]
    return {"ok": not missing, "missing": missing}


def check_write_invariants(db: Session) -> dict[str, Any]:
    """Return whether the hypertable write path is actually usable.

    Existence isn't enough (see module docstring): a table can exist with no
    ``id`` default and no unique index, in which case every insert fails.
    This asserts the two invariants ``0106_restore_hypertable_writes.py``
    established. Postgres-only -- SQLite test DBs never create these
    unmanaged (non-ORM) tables, so there is nothing to check there.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return {"ok": True, "skipped": "non-postgres dialect", "missing_id_default": [], "missing_unique_index": []}
    try:
        inspector = inspect(bind)
        missing_id_default = []
        for table in _ID_DEFAULT_TABLES:
            if not inspector.has_table(table):
                continue
            columns = {c["name"]: c for c in inspector.get_columns(table)}
            id_col = columns.get("id")
            if id_col is not None and not id_col.get("default"):
                missing_id_default.append(table)

        missing_unique_index = []
        for table, index_name in _UNIQUE_INDEXES.items():
            if not inspector.has_table(table):
                continue
            existing_indexes = {ix["name"] for ix in inspector.get_indexes(table)}
            if index_name not in existing_indexes:
                missing_unique_index.append(table)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "missing_id_default": list(_ID_DEFAULT_TABLES),
            "missing_unique_index": list(_UNIQUE_INDEXES),
            "error": repr(exc),
        }

    ok = not missing_id_default and not missing_unique_index
    return {
        "ok": ok,
        "missing_id_default": missing_id_default,
        "missing_unique_index": missing_unique_index,
    }
