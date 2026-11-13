"""Run-time regressions of 2026-11-11: every Discover run hit its 30-minute limit.

* the pooled-ML cache held a session-bound ORM row, so every symbol's ML stage failed;
* a symbol no provider serves was fetched live on every read (about 190 German ETFs);
* the factor-exposure stage was not cached (376 s per run).
"""
from __future__ import annotations

from datetime import UTC, datetime
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


def test_a_symbol_without_bars_is_asked_of_the_providers_once(monkeypatch):
    """The first read asks the provider chain, finds nothing, and every later read (and the ingest) skips it."""
    db = _memory_db()
    registry = MagicMock()
    registry.get_history.return_value = {"ok": False}
    monkeypatch.setattr(market, "build_provider_registry", lambda _db: registry)
    ingests: list[str] = []
    monkeypatch.setattr(DataIngester, "ingest_bar_prices", lambda self, s, **k: ingests.append(s) or {"success": False})
    first = pipeline.stage_history_ingest(db, "NODATA.DE")
    second = pipeline.stage_history_ingest(db, "NODATA.DE")
    assert registry.get_history.call_count == 1
    assert ingests == []
    assert first[0] is None and second[0] is None
    assert second[1].startswith("history_ingest: ")


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


def test_a_miss_survives_a_restart_and_waits_longer_after_repeats():
    from datetime import UTC, datetime, timedelta

    from app.foundation.data_backbone import fetch_misses as fm

    db = _memory_db()
    t0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    assert fm.recently_missed(db, "DEAD.DE", now=t0) == (False, None)
    fm.record_miss(db, "DEAD.DE", "no data", now=t0)
    assert fm.recently_missed(db, "dead.de", now=t0 + timedelta(hours=19)) == (True, "no data")
    assert fm.recently_missed(db, "DEAD.DE", now=t0 + timedelta(hours=25))[0] is False
    # the same day does not count twice; three different days do
    fm.record_miss(db, "DEAD.DE", "no data", now=t0 + timedelta(hours=2))
    fm.record_miss(db, "DEAD.DE", "no data", now=t0 + timedelta(days=1))
    fm.record_miss(db, "DEAD.DE", "no data", now=t0 + timedelta(days=2))
    assert fm.recently_missed(db, "DEAD.DE", now=t0 + timedelta(days=6))[0] is True
    assert fm.recently_missed(db, "DEAD.DE", now=t0 + timedelta(days=10))[0] is False
    fm.clear_miss(db, "DEAD.DE")
    assert fm.recently_missed(db, "DEAD.DE", now=t0 + timedelta(days=2))[0] is False


def test_history_ingest_skips_a_symbol_remembered_from_an_earlier_process(monkeypatch):
    from app.foundation.data_backbone.fetch_misses import record_miss

    db = _memory_db()
    record_miss(db, "OLD.DE", "possibly delisted")
    calls: list[str] = []
    monkeypatch.setattr(DataIngester, "ingest_bar_prices", lambda self, s, **k: calls.append(s) or {"success": False})
    scores, err = pipeline.stage_history_ingest(db, "OLD.DE")
    assert calls == [] and scores is None and "possibly delisted" in err


def test_warm_symbol_uses_the_etf_variant_the_pipeline_asks_for(monkeypatch):
    db = _memory_db()
    seen: list[str] = []
    monkeypatch.setattr(stage_cache, "cached_stage", lambda db, sym, stage, fn, *a, variant="", **k: seen.append(f"{stage}:{variant}"))
    fns = {s: (lambda *a, **k: (None, None)) for s in ("momentum_quality", "backtest_vs_benchmark", "verification_gate", "quant_signals")}
    stage_cache.warm_symbol(db, "IWDA.AS", fns, is_etf=True, asset_class="equity_broad")
    assert "quant_signals:etf=1,class=equity_broad" in seen and "momentum_quality:etf=1,class=-" in seen


def test_alpha_variant_is_read_once_not_per_symbol(monkeypatch):
    db = _memory_db()
    pipeline._ALPHA_VARIANT_MEMO.update(at=0.0, value="")
    n = {"calls": 0}
    monkeypatch.setattr(pipeline, "_library_factors", lambda _db: n.__setitem__("calls", n["calls"] + 1) or [])
    assert pipeline._alpha_variant(db) == pipeline._alpha_variant(db)
    assert n["calls"] == 1


def test_precompute_warms_the_etfs_of_a_cancelled_run(monkeypatch):
    """The run that needs the ETF entries is the one that was cancelled for lacking them."""
    from app.decision.discover import jobs
    from app.foundation.models.entities import DiscoverCandidate, DiscoverRun, User

    db = _memory_db()
    user = User(username="warm-etf", password_hash="x")
    db.add(user)
    db.flush()
    run = DiscoverRun(user_id=user.id, status="cancelled")
    db.add(run)
    db.flush()
    db.add(DiscoverCandidate(run_id=run.id, symbol="ZZETF.DE", name="Test UCITS ETF", source="screen_etf"))
    db.commit()
    warmed: list[tuple[str, bool]] = []

    def fake_warm(_db, symbol, fns, *, is_etf=False, asset_class="equity"):
        warmed.append((symbol, is_etf))
        if is_etf:
            assert "alpha_miner_cached" in fns  # the pipeline runs the miner for ETFs too
        return 1

    monkeypatch.setattr(stage_cache, "warm_symbol", fake_warm)
    monkeypatch.setattr(stage_cache, "prune", lambda _db: 0)
    report = jobs.discover_stage_precompute_inner(db)
    assert ("ZZETF.DE", True) in warmed
    assert report["etfs"] >= 1


def test_latest_bar_date_is_read_once_for_the_stages_of_one_symbol():
    from sqlalchemy import event

    db = _memory_db()
    queries: list[str] = []
    event.listen(db.get_bind(), "before_cursor_execute", lambda *a: queries.append(a[2]))
    for _ in range(5):
        assert stage_cache.latest_bar_date(db, "AAA.DE") is None
    assert sum("bar_prices" in q for q in queries) == 1


def test_todays_partial_bar_does_not_move_the_cache_key():
    from datetime import timedelta

    from sqlalchemy import text

    db = _memory_db()
    now = datetime.now(UTC).replace(tzinfo=None)
    yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for ts in (yesterday, now.replace(hour=0, minute=0, second=0, microsecond=0)):
        db.execute(text("INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, provider) "
                        "VALUES ('AAA.DE', :ts, 1, 1, 1, 1, 1, 't')"), {"ts": ts})
    db.commit()
    assert stage_cache.latest_bar_date(db, "AAA.DE") == str(yesterday)[:10]
