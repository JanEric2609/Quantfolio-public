"""WP9 (plan 2026-11-10): user-independent Discover stages are cached per symbol, stage and bar date."""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from sqlalchemy import text

from app.decision.discover import stage_cache
from tests.test_discover_benchmark_persist import _test_db


def _db(last_bar="2026-11-09"):
    _, factory = _test_db()
    db = factory()
    db.execute(text("INSERT INTO bar_prices (symbol, ts, close) VALUES ('AAA', :a, 1), ('AAA', :b, 2)"),
               {"a": datetime(2026, 11, 6), "b": datetime.fromisoformat(last_bar)})
    db.commit()
    return db


def test_a_second_call_loads_the_cache_and_matches_the_first_exactly():
    db = _db()
    calls: list[int] = []

    def stage(_db, symbol, is_etf=False):
        calls.append(1)
        return {"momentum_12_1m": np.float64(0.25), "pair": (1, 2), "concerns": []}, None

    first = stage_cache.cached_stage(db, "AAA", "momentum_quality", stage, db, "AAA", is_etf=False, variant="v")
    second = stage_cache.cached_stage(db, "AAA", "momentum_quality", stage, db, "AAA", is_etf=False, variant="v")
    assert len(calls) == 1                      # the second run did not compute
    assert first == second == ({"momentum_12_1m": 0.25, "pair": [1, 2], "concerns": []}, None)


def test_a_new_bar_date_or_variant_computes_again():
    db = _db()
    calls: list[int] = []

    def stage(_db, symbol, **_kw):
        calls.append(1)
        return {"v": len(calls)}, None

    stage_cache.cached_stage(db, "AAA", "quant_signals", stage, db, "AAA", variant="etf=0")
    stage_cache.cached_stage(db, "AAA", "quant_signals", stage, db, "AAA", variant="etf=1")
    assert len(calls) == 2
    db.execute(text("INSERT INTO bar_prices (symbol, ts, close) VALUES ('AAA', :a, 3)"), {"a": datetime(2026, 11, 10)})
    db.commit()
    stage_cache.cached_stage(db, "AAA", "quant_signals", stage, db, "AAA", variant="etf=0")
    assert len(calls) == 3                      # a new latest bar is a new key


def test_a_rejection_is_cached_too_and_other_stages_never_are():
    db = _db()
    calls: list[int] = []

    def reject(_db, symbol):
        calls.append(1)
        return None, "verification_gate: volatility 120 % a year exceeds 100 %"

    for _ in range(2):
        assert stage_cache.cached_stage(db, "AAA", "verification_gate", reject, db, "AAA") == (
            None, "verification_gate: volatility 120 % a year exceeds 100 %")
    assert len(calls) == 1
    other = []
    for _ in range(2):
        stage_cache.cached_stage(db, "AAA", "portfolio_fit", lambda *a: (other.append(1), ({}, None))[1], db, "AAA")
    assert len(other) == 2                      # portfolio fit stays on demand


def test_a_symbol_without_bars_computes_and_stores_nothing():
    db = _db()
    calls: list[int] = []
    stage_cache.cached_stage(db, "ZZZ", "momentum_quality", lambda *a: (calls.append(1), ({"x": 1}, None))[1], db, "ZZZ")
    stage_cache.cached_stage(db, "ZZZ", "momentum_quality", lambda *a: (calls.append(1), ({"x": 1}, None))[1], db, "ZZZ")
    assert len(calls) == 2
    assert db.execute(text("SELECT count(*) FROM discover_stage_cache")).scalar() == 0


def test_prune_drops_old_dates_only():
    db = _db()
    db.execute(text("INSERT INTO discover_stage_cache (symbol, stage, bar_date, payload_json, computed_at) "
                    "VALUES ('AAA', 's', '2020-01-01', '[]', :t), ('AAA', 's', :today, '[]', :t)"),
               {"t": datetime.now(UTC), "today": datetime.now(UTC).date().isoformat()})
    db.commit()
    assert stage_cache.prune(db) == 1
    assert db.execute(text("SELECT count(*) FROM discover_stage_cache")).scalar() == 1
