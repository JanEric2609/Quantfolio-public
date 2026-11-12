"""WP7 (plan 2026-11-12): the key a warm-up writes is the key a run reads; the pooled-model cache follows the model."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import text

from app.decision.discover import pipeline, stage_cache
from app.foundation.models.entities import DiscoverStageCache, FactorsLibrary
from tests.test_discover_benchmark_persist import _test_db

STAGES = ("momentum_quality", "backtest_vs_benchmark", "verification_gate", "quant_signals")


def _db():
    _, factory = _test_db()
    db = factory()
    db.execute(text("INSERT INTO bar_prices (symbol, ts, close) VALUES ('AAA', :a, 1), ('AAA', :b, 2)"),
               {"a": datetime(2026, 11, 6), "b": datetime(2026, 11, 9)})
    db.commit()
    stage_cache._BAR_DATE_MEMO.clear()
    pipeline._ALPHA_VARIANT_MEMO.update(at=0.0, value="")
    return db


def _factor(db, ic, icir):
    row = db.query(FactorsLibrary).first()
    if row is None:
        db.add(FactorsLibrary(name="f1", formula_json=json.dumps({"dsl": "rank(close)"}), source="t",
                              ic_summary_json=json.dumps({"ic": ic, "icir": icir})))
    else:
        row.ic_summary_json = json.dumps({"ic": ic, "icir": icir})
    db.commit()
    pipeline._ALPHA_VARIANT_MEMO.update(at=0.0, value="")


@pytest.mark.parametrize("is_etf,asset_class", [(False, "equity"), (True, "bond")])
def test_a_warmed_stage_is_a_hit_for_the_run_that_reads_it(is_etf, asset_class):
    db = _db()
    calls: list[str] = []

    def fake(name):
        def fn(_db, symbol, **_kw):
            calls.append(name)
            return {"stage": name}, None
        return fn

    fns = {name: fake(name) for name in STAGES}
    stored = stage_cache.warm_symbol(db, "AAA", fns, is_etf=is_etf, asset_class=asset_class)
    assert stored == len(STAGES)
    assert sorted(calls) == sorted(STAGES)
    calls.clear()
    # The run's own reader, with the arguments the loop passes (pipeline.py, _cached(...)).
    for name in STAGES:
        kwargs = {"is_etf": is_etf} if name in ("momentum_quality", "verification_gate") \
            else {"is_etf": is_etf, "asset_class": asset_class}
        result = pipeline._cached(db, "AAA", name, fns[name], db, "AAA", **kwargs)
        assert result == ({"stage": name}, None)
    assert calls == []                                  # every read was a hit
    assert db.query(DiscoverStageCache).count() == len(STAGES)


def test_the_miner_key_ignores_ic_rewrites_but_not_a_new_factor():
    db = _db()
    _factor(db, 0.04, 0.5)
    before = pipeline._alpha_variant(db)
    _factor(db, 0.07, 0.9)                              # alphacrafter_daily rewrote the scores at 08:00
    assert pipeline._alpha_variant(db) == before
    db.add(FactorsLibrary(name="f2", formula_json=json.dumps({"dsl": "rank(volume)"}), source="t",
                          ic_summary_json=json.dumps({"ic": 0.03, "icir": 0.4})))
    db.commit()
    pipeline._ALPHA_VARIANT_MEMO.update(at=0.0, value="")
    assert pipeline._alpha_variant(db) != before


def test_a_warmed_miner_entry_is_still_a_hit_after_the_scores_change(monkeypatch):
    db = _db()
    _factor(db, 0.04, 0.5)
    calls: list[int] = []

    def stage(_db, symbol):
        calls.append(1)
        return {"exposure_score": 0.5, "concerns": []}, None

    monkeypatch.setattr(pipeline, "stage_alpha_miner", stage)
    stage_cache.warm_symbol(db, "AAA", {name: (lambda *_a, **_k: ({}, None)) for name in STAGES}
                            | {"alpha_miner_cached": pipeline.stage_alpha_miner_cached})
    assert len(calls) == 1
    _factor(db, 0.09, 1.2)
    pipeline.stage_alpha_miner_cached(db, "AAA")        # the 09:00 run
    assert len(calls) == 1


def test_the_pooled_model_cache_follows_the_model_id(monkeypatch):
    from app.lab.quant_lab import pooled_ml

    class Row:
        def __init__(self, id_, status="candidate"):
            self.id, self.status, self.artefact_path = id_, status, None

    rows = [Row("m1")]
    monkeypatch.setattr(pooled_ml, "latest_pooled_row", lambda _db: rows[0])
    pipeline._ML_CACHE.clear()
    assert pipeline._pooled_serving(None)["model_id"] == "m1"
    rows[0] = Row("m2")                                  # trained later the same day
    assert pipeline._pooled_serving(None)["model_id"] == "m2"
    rows[0] = Row("m2", "completed")                    # then passed its gate
    assert pipeline._pooled_serving(None)["row_status"] == "completed"
    pipeline._ML_CACHE.clear()


def test_non_overlapping_keeps_one_pick_per_symbol_and_holding_period():
    from datetime import UTC, timedelta
    from types import SimpleNamespace as P

    from app.decision.discover.ledger import non_overlapping

    t0 = datetime(2026, 10, 1, tzinfo=UTC)

    def pick(symbol, day, horizon=21):
        at = t0 + timedelta(days=day)
        return P(symbol=symbol, horizon_days=horizon, predicted_at=at, resolve_at=at + timedelta(days=horizon * 7 // 5))

    picks = [pick("AAA", d) for d in (0, 1, 2, 10, 29, 30)] + [pick("BBB", 1), pick("AAA", 3, horizon=5)]
    kept = non_overlapping(picks)
    kept_days = sorted((k.symbol, k.horizon_days, (k.predicted_at - t0).days) for k in kept)
    assert kept_days == [("AAA", 5, 3), ("AAA", 21, 0), ("AAA", 21, 29), ("BBB", 21, 1)]


def test_a_fund_class_never_takes_more_than_the_sector_cap_of_the_shortlist():
    def fund(sym, score, klass):
        return {"symbol": sym, "lane": "fund", "composite_score": score, "asset_class": klass, "scores": {}}

    ranked = [fund("CN1", .9, "bond_govt_cn"), fund("CN2", .8, "bond_govt_cn"), fund("CN3", .7, "bond_govt_cn"),
              fund("EQ1", .6, "equity"), fund("NOCLASS", .5, None)]
    selected, _ = pipeline.select_shortlist(ranked, 11)
    symbols = {r["symbol"] for r in selected}
    assert symbols == {"CN1", "CN2", "EQ1", "NOCLASS"} - set() or "CN3" not in symbols
    assert "CN3" not in symbols and ranked[2].get("class_capped") is True
