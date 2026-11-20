"""Trial ledger for AlphaCrafter factor evaluations (M3a).

Audit F13 (docs/archive/audits/2026-08-discover-maths): the discover alpha-miner
stage aggregated ``abs(ic)``, so a strongly anti-predictive factor scored
identically to a predictive one and corrupted composite scoring (composite.py
treats ``ic`` as signed). This module persists SIGNED IC/ICIR trials to
``ac_trial_ledger`` with DB-level idempotency so retried runs cannot
double-count evidence.

Oracle-verified design corrections vs. the blueprint appendix M3 section:
unique key ``(run_id, factor_name, symbol, config_hash)`` WITHOUT
``evaluated_at``; ``run_id`` NOT NULL; canonical sha256 config hash;
portable SELECT-then-INSERT with an IntegrityError race backstop.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.foundation.models.entities import AcTrialLedger

_CONFIG_HASH_LEN = 64


def canonical_config_hash(factor_def: Mapping[str, Any], horizon_days: int) -> str:
    """Deterministic sha256 identity of a factor definition + horizon.

    Hashes ``{name, formula, source, params-if-present, horizon_days}`` with
    sorted JSON keys so key order never changes the hash; ``params`` only
    participates when the definition actually carries one.
    """
    payload: dict[str, Any] = {
        "name": factor_def.get("name"),
        "formula": factor_def.get("formula"),
        "source": factor_def.get("source"),
        "horizon_days": horizon_days,
    }
    params = factor_def.get("params")
    if params is not None:
        payload["params"] = params
    # sort_keys gives deterministic hashing for JSON-safe params; default=str
    # is a fallback for scalars only — set/frozenset params would stringify in
    # insertion order and hash differently across processes (unreachable with
    # the code-owned factor defs today, but do not pass sets here).
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_CONFIG_HASH_LEN]


def append_trial(
    db: Session,
    *,
    run_id: str,
    factor_name: str,
    symbol: str,
    ic: float,
    icir: float | None,
    n_obs: int,
    horizon_days: int,
    config_hash: str,
    evaluated_at: datetime | None = None,
) -> bool:
    """Append one trial row; return True when written, False when duplicate.

    Portable across SQLite and PostgreSQL: existence is checked with a plain
    SELECT on the unique key first, then INSERT; an IntegrityError (concurrent
    writer won the race under the ``uq_ac_trial_append`` unique index) is
    rolled back and reported as False rather than raised.
    """
    exists = db.scalar(
        select(AcTrialLedger.id).where(
            AcTrialLedger.run_id == run_id,
            AcTrialLedger.factor_name == factor_name,
            AcTrialLedger.symbol == symbol,
            AcTrialLedger.config_hash == config_hash,
        )
    )
    if exists is not None:
        return False

    row = AcTrialLedger(
        run_id=run_id,
        factor_name=factor_name,
        symbol=symbol,
        ic=ic,
        icir=icir,
        n_obs=n_obs,
        horizon_days=horizon_days,
        config_hash=config_hash,
        evaluated_at=evaluated_at or datetime.now(UTC),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def list_trials(
    db: Session,
    config_hash: str | None = None,
    factor_name: str | None = None,
    symbol: str | None = None,
    run_id: str | None = None,
    limit: int = 1000,
) -> list[AcTrialLedger]:
    """Query trial rows with optional filters, oldest first."""
    stmt = select(AcTrialLedger)
    if config_hash is not None:
        stmt = stmt.where(AcTrialLedger.config_hash == config_hash)
    if factor_name is not None:
        stmt = stmt.where(AcTrialLedger.factor_name == factor_name)
    if symbol is not None:
        stmt = stmt.where(AcTrialLedger.symbol == symbol)
    if run_id is not None:
        stmt = stmt.where(AcTrialLedger.run_id == run_id)
    stmt = stmt.order_by(AcTrialLedger.evaluated_at, AcTrialLedger.id).limit(limit)
    return list(db.scalars(stmt))


def family_size(db: Session, factor_names: Sequence[str] | None = None) -> int:
    """Count DISTINCT config hashes in the ledger, optionally per factor set."""
    stmt = select(func.count(func.distinct(AcTrialLedger.config_hash)))
    if factor_names:
        stmt = stmt.where(AcTrialLedger.factor_name.in_(list(factor_names)))
    return int(db.scalar(stmt) or 0)
