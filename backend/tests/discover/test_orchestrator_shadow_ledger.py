"""A full Discover run writes the shadow ledger and stamps its predictions (ADR 0018).

Drives ``_run_discover_inner`` with the universe, pipeline, tradeability and
dossier writer stubbed at the orchestrator's own seams; everything else
(configs, the prediction ledger, the snapshot writer, calibration) is real.
"""
from __future__ import annotations

import json
import uuid
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import orchestrator, shadow_ledger
from app.foundation import provenance
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    DiscoverCandidateSnapshot,
    DiscoverRun,
    DiscoveryPrediction,
    User,
)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _result(symbol: str, composite: float, reject: str | None = None) -> dict:
    return {
        "symbol": symbol, "isin": None, "name": symbol, "source": "screen_equity",
        "scores": {"sentiment_fundamentals": {"sector": f"S-{symbol}"}, "composite_raw": composite},
        "concerns": [], "reject_stage": reject, "reject_reason": "x" if reject else None,
        "composite_score": composite if reject is None else 0.0,
    }


def test_a_run_freezes_every_candidate_and_stamps_each_prediction(monkeypatch):
    db = _memory_db()
    user = User(username="runner", password_hash="x")
    db.add(user)
    db.commit()
    run = DiscoverRun(id=str(uuid.uuid4()), user_id=user.id, status="queued", stage_json="{}", params_json="{}")
    db.add(run)
    db.commit()

    universe = [{"symbol": s, "source": "screen_equity", "name": s} for s in ("AAA", "BBB", "CCC", "DDD")]
    results = [_result("AAA", 0.8), _result("BBB", 0.6), _result("CCC", 0.4), _result("DDD", 0.0, "history_ingest")]

    def fake_pipeline(db, user_id, universe, profile, *, results_out, **kwargs):
        results_out.extend(results)
        yield {"index": 1, "total": 4, "symbol": "AAA", "stage": "completed"}
        return [results[0], results[1]]  # the top-2 shortlist

    def fake_dossier(db, cand, profile, **kwargs):
        return {"dossier_id": None, "generated_by": "template", "fallback_reason": "test", "dossier": {}}

    class _NeverCancelled:  # the real token re-reads the run on its own session
        def __init__(self, db, run_id):
            pass

        def raise_if_cancelled(self):
            return None

    monkeypatch.setattr(orchestrator, "CancellationToken", _NeverCancelled)
    monkeypatch.setattr(orchestrator, "build_universe", lambda db, uid, focus=None, registry=None: universe)
    monkeypatch.setattr(orchestrator, "build_provider_registry", lambda db: None)
    monkeypatch.setattr(orchestrator, "run_candidate_pipeline", fake_pipeline)
    monkeypatch.setattr(orchestrator, "assess_tradeability", lambda *a, **k: {"likely_tradeable": True})
    monkeypatch.setattr(orchestrator, "write_dossier", fake_dossier)
    monkeypatch.setattr(orchestrator, "attach_earnings_calendar", lambda db, symbol, scores: scores)
    monkeypatch.setattr(orchestrator, "ledger_return_range", lambda *a, **k: (None, None))
    monkeypatch.setattr(provenance, "llm_server_identity", lambda db=None, refresh=False: {"available": False, "error": "offline"})
    monkeypatch.setattr(shadow_ledger, "history", lambda db, s, days, allow_live: [{"date": date(2026, 10, 2), "close": 5.0}])

    orchestrator._run_discover_inner(db, run.id)

    assert db.get(DiscoverRun, run.id).status == "completed"
    snaps = {s.symbol: s for s in db.query(DiscoverCandidateSnapshot).filter_by(run_id=run.id).all()}
    assert set(snaps) == {"AAA", "BBB", "CCC", "DDD"}
    assert (snaps["AAA"].stock_rank, snaps["BBB"].stock_rank, snaps["CCC"].stock_rank) == (1, 2, 3)
    assert snaps["AAA"].shortlisted and snaps["BBB"].shortlisted and not snaps["CCC"].shortlisted
    # Below 15 tradeable names the orchestrator backfills from the next ranks:
    # CCC was not in the pipeline's shortlist but was picked.
    assert snaps["AAA"].picked and snaps["BBB"].picked and snaps["CCC"].picked
    assert snaps["DDD"].evaluable is False and not snaps["DDD"].picked
    cohort = snaps["AAA"].cohort_id
    assert cohort.startswith("c1-")

    preds = db.query(DiscoveryPrediction).filter_by(run_id=run.id).all()
    assert {p.symbol for p in preds} == {"AAA", "BBB", "CCC"}
    for p in preds:
        stamp = p.provenance_json
        assert stamp["cohort_id"] == cohort and stamp["source"] == "discover"
        assert stamp["dossier_generated_by"] == "template"
        assert {"dossier_not_llm", "llm_identity_unavailable"} <= set(stamp["degradation_flags"])
        assert json.dumps(stamp)  # plain JSON, storable as-is
