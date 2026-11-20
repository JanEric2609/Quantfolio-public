"""End-to-end integration test for the Discovery P2 pipeline.

Exercises the cross-component data flow:
- Candidate pipeline results → composite scoring → tradeability gate
- Dossier generation (with mocked LLM) → §5 schema persistence
- Prediction ledger writes
- API responses reflect the new §5 structured schema + dossier-derived fields

All tests use in-memory SQLite and mock external calls (LLM, price cache).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from uuid import uuid4

from conftest import _memory_db
from sqlalchemy.orm import Session

from app.interface.api.discover import DossierResponse, get_dossier, get_run
from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
    DiscoveryPrediction,
    RecommendationDossier,
    User,
)
from app.decision.discover.dossier_writer import write_dossier
from app.decision.discover.ledger import write_predictions


def _user(db: Session, name: str = "alice") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_scores(composite: float = 0.65) -> dict[str, Any]:
    return {
        "composite": composite,
        "alpha_miner": {
            "ic": 0.72,
            "icir": 1.8,
        },
        "alpha_screener": {
            "regime_affinity": 0.60,
        },
        "sentiment_fundamentals": {
            "sentiment_score": 0.55,
            "fundamentals_score": 0.50,
            "analyst_estimate_score": 0.58,
        },
        "portfolio_fit": {
            "fit_score": 0.70,
        },
        # Quant anchor for the deterministic expected_return computation
        # (Phase 3, app.foundation.expected_return) — without this,
        # expected_return is correctly None (no anchor, no invented number).
        "backtest_vs_benchmark": {"candidate_return_annual": 0.15},
    }


def _candidate_dict(
    symbol: str,
    *,
    run_id: str,
    scores: dict[str, Any] | None = None,
    tradeable: bool = True,
) -> dict[str, Any]:
    return {
        "id": uuid4().hex,
        "run_id": run_id,
        "symbol": symbol,
        "isin": f"US{symbol}001",
        "name": f"{symbol} Inc.",
        "source": "screen_index",
        "status": "shortlisted",
        "reject_stage": None,
        "reject_reason": None,
        "scores_json": json.dumps(scores or _make_scores()),
        "tradeable_json": json.dumps({"likely_tradeable": tradeable, "confidence": "high"}),
        "dossier_id": None,
        "recommendation_id": None,
    }


# Deterministic LLM response matching the §5 schema
_DETERMINISTIC_DOSSIER_JSON = json.dumps({
    "direction": "long",
    "conviction": 0.78,
    "horizon_months": 6,
    "expected_return": 14.2,
    "thesis": "Strong quantitative signals support a positive outlook.",
    "key_risks": ["Market volatility", "Sector rotation risk"],
})


def _mock_llm(db: Any, messages: list[dict], timeout_s: float = 120.0, **_kwargs: Any) -> str:
    return _DETERMINISTIC_DOSSIER_JSON


class TestE2EPipeline:
    """Full end-to-end: scores → dossier → prediction → API."""

    def test_full_pipeline_data_flow(self, monkeypatch: Any) -> None:
        monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_sentiment",
                           lambda symbol, db: {"news_sentiment": 0.5, "social_mentions": 0, "sentiment_trend": "stable"})
        monkeypatch.setattr("app.decision.discover.dossier_writer._fetch_fundamentals",
                           lambda symbol, db: None)
        monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync",
                           lambda db, msgs, timeout_s=120.0, **_kw: _DETERMINISTIC_DOSSIER_JSON)
        monkeypatch.setattr("app.decision.discover.ledger.latest_cached_close",
                           lambda symbol, db, as_of=None: 150.0)
        db = _memory_db()
        user = _user(db)
        run_id = uuid4().hex

        # ------------------------------------------------------------------
        # 1. Create a run with shortlisted candidates + pipeline results
        # ------------------------------------------------------------------
        run = DiscoverRun(
            id=run_id,
            user_id=user.id,
            status="running",
            stage_json="{}",
            params_json="{}",
        )
        db.add(run)

        # Tradeable candidate — should get dossier + prediction
        cand_tradeable = DiscoverCandidate(**_candidate_dict("AAPL", run_id=run_id))
        # Non-tradeable candidate — should be rejected by tradeability gate
        cand_nontradeable = DiscoverCandidate(
            **_candidate_dict("MSFT", run_id=run_id, tradeable=False)
        )
        # Tradeable ETF — tests the full flow with a different source
        cand_etf = DiscoverCandidate(
            **_candidate_dict("VWRA", run_id=run_id, scores=_make_scores(composite=0.70), tradeable=True)
        )
        for c in [cand_tradeable, cand_nontradeable, cand_etf]:
            db.add(c)
        db.commit()
        for c in [cand_tradeable, cand_nontradeable, cand_etf]:
            db.refresh(c)

        # Simulate pipeline results (as written by orchestrator._run_discover_inner)
        scores_tradeable = _make_scores(composite=0.65)
        scores_etf = _make_scores(composite=0.70)
        pipeline_results = [
            {"symbol": "AAPL", "scores": scores_tradeable, "composite_score": 0.65, "isin": "USAAPL001",
             "concerns": [], "source": "screen_index"},
            {"symbol": "MSFT", "scores": _make_scores(composite=0.55), "composite_score": 0.55, "isin": "USMSFT001",
             "concerns": [], "source": "screen_index"},
            {"symbol": "VWRA", "scores": scores_etf, "composite_score": 0.70, "isin": "USVWRA001",
             "concerns": [], "source": "screen_index"},
        ]

        # ------------------------------------------------------------------
        # 2. Tradeability gate (simulating orchestrator step 3b)
        # ------------------------------------------------------------------
        tradeable_shortlisted = []
        for cand in [cand_tradeable, cand_etf]:
            t = json.loads(cand.tradeable_json) if cand.tradeable_json else {}
            if t.get("likely_tradeable") is not False:
                tradeable_shortlisted.append(cand)
        assert len(tradeable_shortlisted) == 2  # AAPL + VWRA (MSFT excluded)

        # ------------------------------------------------------------------
        # 3. Dossier generation for tradeable candidates
        # ------------------------------------------------------------------
        profile = {"currency": "EUR", "risk_profile": "moderate", "tax_residency_country": "DE"}
        dossier_ids: list[str] = []

        for cand in tradeable_shortlisted:
            cand_dict = {
                "id": cand.id,
                "run_id": cand.run_id,
                "symbol": cand.symbol,
                "isin": cand.isin,
                "name": cand.name,
                "source": cand.source,
                "status": cand.status,
                "reject_stage": cand.reject_stage,
                "reject_reason": cand.reject_reason,
                "scores_json": cand.scores_json,
                "tradeable_json": cand.tradeable_json,
                "dossier_id": cand.dossier_id,
                "recommendation_id": cand.recommendation_id,
            }
            result = write_dossier(db, cand_dict, profile)
            assert result.get("dossier_id") is not None
            assert result.get("recommendation_id") is not None
            dossier_ids.append(result["dossier_id"])

            # Link dossier to candidate (as orchestrator does)
            cand.dossier_id = result["dossier_id"]
            cand.recommendation_id = result.get("recommendation_id")

        db.commit()
        assert len(dossier_ids) == 2

        # ------------------------------------------------------------------
        # 4. Prediction ledger
        # ------------------------------------------------------------------
        predictions = write_predictions(
            db,
            run_id=run_id,
            user_id=user.id,
            shortlisted=[pipeline_results[0], pipeline_results[2]],  # AAPL + VWRA (MSFT excluded)
        )
        assert len(predictions) == 2  # AAPL + VWRA
        assert all(p.outcome_status == "pending" for p in predictions)

        # Refresh to pick up dossier links
        db.refresh(cand_tradeable)
        db.refresh(cand_etf)

        # ------------------------------------------------------------------
        # 5. Verify DB state
        # ------------------------------------------------------------------

        # 5a. Candidates
        assert cand_tradeable.status == "shortlisted"
        assert cand_tradeable.dossier_id is not None
        assert cand_tradeable.recommendation_id is not None

        assert cand_nontradeable.status == "shortlisted"  # status unchanged (gate is in orchestrator)
        assert cand_nontradeable.dossier_id is None  # no dossier generated

        assert cand_etf.dossier_id is not None
        assert cand_etf.recommendation_id is not None

        # 5b. Dossiers — verify §5 schema
        for did in dossier_ids:
            dossier = db.get(RecommendationDossier, did)
            assert dossier is not None
            d_json = json.loads(dossier.dossier_json)
            assert d_json["direction"] == "long"
            assert 0 <= d_json["conviction"] <= 1
            assert 1 <= d_json["horizon_months"] <= 12
            assert d_json["estimate"] is True
            assert d_json["not_financial_advice"] is True
            assert isinstance(d_json["thesis"], str) and len(d_json["thesis"]) > 0
            assert isinstance(d_json["key_risks"], list)
            assert isinstance(d_json["signal_breakdown"], dict)
            assert "composite_score" in d_json["signal_breakdown"]

        # 5c. Predictions
        aapl_pred = db.query(DiscoveryPrediction).filter_by(symbol="AAPL").first()
        assert aapl_pred is not None
        assert aapl_pred.conviction == 0.65  # from composite
        assert aapl_pred.price_at_prediction == 150.0
        assert aapl_pred.is_estimate is True
        assert aapl_pred.is_financial_advice is False

        vwra_pred = db.query(DiscoveryPrediction).filter_by(symbol="VWRA").first()
        assert vwra_pred is not None
        assert vwra_pred.conviction == 0.70

        # ------------------------------------------------------------------
        # 6. API end-to-end
        # ------------------------------------------------------------------

        # 6a. get_run returns candidates with dossier-derived fields
        run.status = "completed"
        db.commit()
        run_resp = get_run(run_id, db=db, user=user)
        assert len(run_resp.candidates) == 3

        aapl_cand = next(c for c in run_resp.candidates if c.symbol == "AAPL")
        assert aapl_cand.conviction is not None  # dossier-derived
        assert aapl_cand.horizon_months is not None
        assert aapl_cand.expected_return is not None
        assert aapl_cand.dossier_id is not None

        msft_cand = next(c for c in run_resp.candidates if c.symbol == "MSFT")
        assert msft_cand.dossier_id is None
        assert msft_cand.conviction is None  # no dossier → no derived fields

        # 6b. get_dossier returns §5 DossierResponse
        dossier_resp = get_dossier(dossier_ids[0], db=db, user=user)
        assert isinstance(dossier_resp, DossierResponse)
        assert dossier_resp.direction == "long"
        assert dossier_resp.conviction == 0.65  # from composite score, not LLM output
        assert dossier_resp.horizon_months == 6
        # expected_return is never taken from the LLM (14.2 above) — it's
        # always derived deterministically from the quant anchor. ADR 0017:
        # prior 2.2% + 1.0 x 4.5% = 6.7%, tilt 0.10 * (15.0 - 6.7) = 0.83,
        # at horizon 6mo -> 7.53 * 0.5 = 3.8
        assert dossier_resp.expected_return == pytest.approx(3.8, abs=0.05)
        assert "quantitative" in dossier_resp.thesis
        assert len(dossier_resp.key_risks) == 2
        assert dossier_resp.estimate is True
        assert dossier_resp.not_financial_advice is True
        assert dossier_resp.created_at is not None

        # 6c. VWRA dossier has its own fields
        vwra_resp = get_dossier(dossier_ids[1], db=db, user=user)
        assert vwra_resp.direction == "long"

    @patch("app.decision.discover.dossier_writer._local_llm_sync", lambda m, t, **_kw: "invalid json")
    def test_dossier_llm_failure_falls_back_to_deterministic(self) -> None:
        """When LLM returns invalid JSON, the deterministic template produces §5 output."""
        db = _memory_db()
        user = _user(db)
        run_id = uuid4().hex

        cand = DiscoverCandidate(**(_candidate_dict("AAPL", run_id=run_id)))
        db.add(cand)
        db.commit()
        db.refresh(cand)

        cand_dict = {
            "id": cand.id,
            "run_id": cand.run_id,
            "symbol": cand.symbol,
            "isin": cand.isin,
            "name": cand.name,
            "source": cand.source,
            "status": cand.status,
            "scores_json": cand.scores_json,
            "tradeable_json": cand.tradeable_json,
            "dossier_id": cand.dossier_id,
            "recommendation_id": cand.recommendation_id,
        }

        profile = {"currency": "EUR", "risk_profile": "moderate"}
        result = write_dossier(db, cand_dict, profile)

        assert result.get("dossier_id") is not None
        dossier = db.get(RecommendationDossier, result["dossier_id"])
        assert dossier is not None
        d_json = json.loads(dossier.dossier_json) if isinstance(dossier.dossier_json, str) else dossier.dossier_json
        # Deterministic template should still produce §5 fields
        assert d_json.get("direction") in ("long", "short", "neutral")
        assert isinstance(d_json.get("conviction"), (int, float))
        assert isinstance(d_json.get("thesis"), str)
        assert isinstance(d_json.get("key_risks"), list)
        assert d_json.get("estimate") is True
        assert d_json.get("not_financial_advice") is True

    def test_pipeline_handles_tradeability_gate_correctly(self, monkeypatch: Any) -> None:
        monkeypatch.setattr("app.decision.discover.dossier_writer._local_llm_sync", _mock_llm)
        monkeypatch.setattr("app.decision.discover.ledger.latest_cached_close",
                           lambda symbol, db, as_of=None: 200.0)
        """Non-tradeable candidates are excluded from dossier + prediction flow."""
        db = _memory_db()
        user = _user(db)
        run_id = uuid4().hex

        run = DiscoverRun(id=run_id, user_id=user.id, status="running", stage_json="{}", params_json="{}")
        db.add(run)

        # One tradeable, one not
        t = DiscoverCandidate(**(_candidate_dict("AAPL", run_id=run_id, tradeable=True)))
        nt = DiscoverCandidate(**(_candidate_dict("MSFT", run_id=run_id, tradeable=False)))
        db.add_all([t, nt])
        db.commit()

        # Simulate tradeability gate
        gate_passes = []
        for c in [t, nt]:
            t_json = json.loads(c.tradeable_json) if c.tradeable_json else {}
            if t_json.get("likely_tradeable") is not False:
                gate_passes.append(c)
        assert len(gate_passes) == 1
        assert gate_passes[0].symbol == "AAPL"

        # Only the tradeable one gets a dossier
        profile = {"currency": "EUR", "risk_profile": "moderate", "tax_residency_country": "DE"}
        cand_dict = {
            "id": t.id, "run_id": t.run_id, "symbol": t.symbol, "isin": t.isin, "name": t.name,
            "source": t.source, "status": t.status, "reject_stage": None, "reject_reason": None,
            "scores_json": t.scores_json, "tradeable_json": t.tradeable_json,
            "dossier_id": None, "recommendation_id": None,
        }
        result = write_dossier(db, cand_dict, profile)
        assert result.get("dossier_id") is not None
        t.dossier_id = result["dossier_id"]
        t.recommendation_id = result.get("recommendation_id")
        db.commit()
    
        # Verify only AAPL got a dossier via API
        run.status = "completed"
        db.commit()
        run_resp = get_run(run_id, db=db, user=user)
        aapl = next(c for c in run_resp.candidates if c.symbol == "AAPL")
        msft = next(c for c in run_resp.candidates if c.symbol == "MSFT")
        assert aapl.dossier_id is not None
        assert aapl.conviction is not None
        assert msft.dossier_id is None
        assert msft.conviction is None
