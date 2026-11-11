"""Run-time regressions of 2026-11-11: every Discover run hit its 30-minute limit.

* the pooled-ML cache held a session-bound ORM row, so every symbol's ML stage failed;
* a symbol no provider serves was fetched live on every read (about 190 German ETFs);
* the factor-exposure stage was not cached (376 s per run).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from conftest import _memory_db

from app.decision.discover import pipeline, stage_cache
from app.foundation import market
from app.foundation.data_backbone.ingest import DataIngester


def test_ml_cache_holds_plain_values_not_an_orm_row(monkeypatch):
    db = _memory_db()
    row = MagicMock(id="m1", status="rejected_below_gate", artefact_path=None)
    monkeypatch.setattr("app.lab.quant_lab.pooled_ml.latest_pooled_row", lambda _db: row)
    state = dict(pipeline._pooled_serving(db))
    pipeline._ML_CACHE.clear()
    assert "row" not in state
    assert state["model_id"] == "m1"


def test_a_symbol_without_bars_is_fetched_once_per_window(monkeypatch):
    db = _memory_db()
    calls: list[str] = []

    def fail(self, symbol, **kwargs):
        calls.append(symbol)
        return {"success": False, "message": "no data from any provider"}

    monkeypatch.setattr(DataIngester, "ingest_bar_prices", fail)
    first = pipeline.stage_history_ingest(db, "NODATA.DE")
    second = pipeline.stage_history_ingest(db, "NODATA.DE")
    assert calls == ["NODATA.DE"]
    assert first[1] == second[1]
    assert second[0] is None and second[1].startswith("history_ingest: ")


def test_live_history_miss_is_remembered(monkeypatch):
    db = _memory_db()
    registry = MagicMock()
    registry.get_history.return_value = {"ok": False}
    monkeypatch.setattr(market, "build_provider_registry", lambda _db: registry)
    assert market.history(db, "UC85.DE", days=30) == []
    assert market.history(db, "UC85.DE", days=30) == []
    assert registry.get_history.call_count == 1


def test_alpha_miner_stage_is_cached_and_errors_are_not(monkeypatch):
    from sqlalchemy import text

    db = _memory_db()
    monkeypatch.setattr(stage_cache, "latest_bar_date", lambda _db, _symbol: "2026-11-10")
    n = {"calls": 0}

    def stage(_db, symbol, *, run_id=None):
        n["calls"] += 1
        return {"exposure_score": 0.6, "concerns": []}, None

    monkeypatch.setattr(pipeline, "stage_alpha_miner", stage)
    a = pipeline.stage_alpha_miner_cached(db, "AAA")
    b = pipeline.stage_alpha_miner_cached(db, "AAA")
    assert a == b and n["calls"] == 1

    def broken(_db, symbol, *, run_id=None):
        n["calls"] += 1
        return {"exposure_score": None, "concerns": ["alphacrafter_miner_error: boom"]}, None

    monkeypatch.setattr(pipeline, "stage_alpha_miner", broken)
    db.execute(text("DELETE FROM discover_stage_cache"))
    db.commit()
    before = n["calls"]
    pipeline.stage_alpha_miner_cached(db, "AAA")
    pipeline.stage_alpha_miner_cached(db, "AAA")
    assert n["calls"] == before + 2
    assert "alpha_miner" in stage_cache.CACHEABLE_STAGES
