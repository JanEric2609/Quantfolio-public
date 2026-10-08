"""Phase 6: Integration and edge-case tests for the Discover pipeline.

Covers:
- API endpoint integration (dossier returns Phase 5 fields, security, filtering)
- Edge cases (empty candidates, all-rejected, missing/partial data, degraded providers)
- Cross-component data flow (dossier JSON → API, composite scoring with missing stages,
  portfolio fit fail-open)

All tests use in-memory SQLite and mock external network calls. No asyncio.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from conftest import _memory_db
from fastapi import HTTPException

from app.interface.api.discover import (
    DossierResponse,
    _safe_json_loads,
    get_dossier,
    get_run,
    list_runs,
)
from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
    RecommendationDossier,
    User,
)
from app.decision.discover import (
    DegradationAssessor,
)
from app.decision.discover.dossier_writer import write_dossier
from app.decision.discover.pipeline import (
    _preingest_candidates,
    run_candidate_pipeline,
)


def _user(db, name="alice") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_dossier(
    db,
    dossier_json: dict | None = None,
    conviction: float = 0.75,
) -> RecommendationDossier:
    dossier = RecommendationDossier(
        id=uuid4().hex,
        conviction=conviction,
        dossier_json=json.dumps(
            dossier_json
            if dossier_json is not None
            else {
                "direction": "long",
                "conviction": conviction,
                "horizon_months": 6,
                "expected_return": 12.5,
                "thesis": "Buy AAPL based on strong quantitative signals.",
                "key_risks": ["Market risk", "Concentration risk"],
                "signal_breakdown": {},
                "estimate": True,
                "not_financial_advice": True,
            }
        ),
    )
    db.add(dossier)
    db.commit()
    db.refresh(dossier)
    return dossier


def _seed_run(
    db,
    user_id: str,
    *,
    status: str = "completed",
    candidates: list[dict] | None = None,
) -> DiscoverRun:
    run = DiscoverRun(
        id=uuid4().hex,
        user_id=user_id,
        status=status,
        stage_json=json.dumps({"discover": {"state": "completed", "message": "Done"}}),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    if candidates:
        for c in candidates:
            db.add(
                DiscoverCandidate(
                    id=uuid4().hex,
                    run_id=run.id,
                    symbol=c.get("symbol", "AAPL"),
                    isin=c.get("isin"),
                    name=c.get("name", c.get("symbol", "AAPL")),
                    source=c.get("source", "screen_index"),
                    status=c.get("status", "shortlisted"),
                    reject_stage=c.get("reject_stage"),
                    reject_reason=c.get("reject_reason"),
                    scores_json=json.dumps(c.get("scores", {})),
                    tradeable_json=json.dumps(c.get("tradeable", {})),
                    dossier_id=c.get("dossier_id"),
                    recommendation_id=c.get("recommendation_id"),
                )
            )
        db.commit()
    db.refresh(run)
    return run


# ======================================================================
# Section 1 — API endpoint integration
# ======================================================================


class TestApiGetDossier:
    """Integration: GET /api/discover/dossiers/{id} returns Phase 5 fields."""

    def test_dossier_returns_section5_fields(self):
        """dossier_json with §5 schema → API surfaces all structured fields."""
        db = _memory_db()
        user = _user(db, "alice")
        dossier = _seed_dossier(db)

        # Link candidate to dossier and user
        run = DiscoverRun(id=uuid4().hex, user_id=user.id, status="completed")
        db.add(run)
        db.commit()
        db.add(
            DiscoverCandidate(
                id=uuid4().hex,
                run_id=run.id,
                symbol="AAPL",
                source="screen_index",
                status="shortlisted",
                dossier_id=dossier.id,
            )
        )
        db.commit()

        result = get_dossier(dossier.id, db=db, user=user)

        assert isinstance(result, DossierResponse)
        assert result.id == dossier.id
        assert result.direction == "long"
        assert result.horizon_months == 6
        assert result.expected_return == 12.5
        assert result.thesis == "Buy AAPL based on strong quantitative signals."
        assert result.key_risks == ["Market risk", "Concentration risk"]
        assert result.estimate is True
        assert result.not_financial_advice is True

    def test_dossier_missing_section5_fields_defaults_safely(self):
        """Minimal dossier JSON without §5 keys → safe defaults, no crash."""
        db = _memory_db()
        user = _user(db, "alice")
        dossier = _seed_dossier(
            db,
            dossier_json={"thesis": "Old dossier"},
        )

        run = DiscoverRun(id=uuid4().hex, user_id=user.id, status="completed")
        db.add(run)
        db.commit()
        db.add(
            DiscoverCandidate(
                id=uuid4().hex,
                run_id=run.id,
                symbol="OLD",
                source="screen_index",
                status="shortlisted",
                dossier_id=dossier.id,
            )
        )
        db.commit()

        result = get_dossier(dossier.id, db=db, user=user)

        assert result.thesis == "Old dossier"
        assert result.direction == "neutral"
        assert result.horizon_months == 6
        assert result.expected_return is None
        assert result.key_risks == []

    def test_dossier_access_denied_for_wrong_owner(self):
        """User B cannot see User A's dossier (404, not 403, to avoid leaking)."""
        db = _memory_db()
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        dossier = _seed_dossier(db)

        run = DiscoverRun(id=uuid4().hex, user_id=alice.id, status="completed")
        db.add(run)
        db.commit()
        db.add(
            DiscoverCandidate(
                id=uuid4().hex,
                run_id=run.id,
                symbol="AAPL",
                source="screen_index",
                status="shortlisted",
                dossier_id=dossier.id,
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as exc:
            get_dossier(dossier.id, db=db, user=bob)
        assert exc.value.status_code == 404


class TestApiGetRun:
    """Integration: GET /api/discover/runs/{id} splits candidates correctly."""

    def test_get_run_splits_shortlisted_and_rejected(self):
        db = _memory_db()
        user = _user(db)
        run = _seed_run(
            db,
            user.id,
            candidates=[
                {
                    "symbol": "AAPL",
                    "source": "screen_index",
                    "status": "shortlisted",
                    "scores": {"composite": 0.85},
                },
                {
                    "symbol": "BAD",
                    "source": "screen_index",
                    "status": "rejected",
                    "reject_stage": "momentum_quality",
                    "reject_reason": "negative momentum",
                    "scores": {"composite": 0.15},
                },
                {
                    "symbol": "MSFT",
                    "source": "screen_etf",
                    "status": "shortlisted",
                    "scores": {"composite": 0.88},
                },
            ],
        )

        result = get_run(run.id, db=db, user=user)

        assert len(result.candidates) == 3
        assert len(result.shortlisted) == 2
        assert len(result.rejected) == 1
        assert {c.symbol for c in result.shortlisted} == {"AAPL", "MSFT"}
        assert result.rejected[0].symbol == "BAD"
        assert result.rejected[0].reject_stage == "momentum_quality"

    def test_get_run_owner_only(self):
        db = _memory_db()
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        run = _seed_run(db, alice.id)

        with pytest.raises(HTTPException) as exc:
            get_run(run.id, db=db, user=bob)
        assert exc.value.status_code == 404

    def test_get_run_includes_dossier_derived_fields(self):
        """Shortlisted candidate linked to a §5 dossier exposes conviction/horizon/E[return]."""
        db = _memory_db()
        user = _user(db)
        dossier = _seed_dossier(db)
        run = _seed_run(
            db,
            user.id,
            candidates=[
                {
                    "symbol": "AAPL",
                    "source": "screen_index",
                    "status": "shortlisted",
                    "dossier_id": dossier.id,
                    "scores": {"composite": 0.85},
                }
            ],
        )

        result = get_run(run.id, db=db, user=user)

        assert len(result.shortlisted) == 1
        cand = result.shortlisted[0]
        assert cand.dossier_id == dossier.id
        assert cand.conviction == 0.75
        assert cand.horizon_months == 6
        assert cand.expected_return == 12.5


    def test_get_run_carries_the_frozen_pool_rank(self):
        """The shadow ledger's rank among the run's evaluable stocks rides on the candidate (ADR 0018 §6)."""
        from app.foundation.models.entities import DiscoverCandidateSnapshot

        db = _memory_db()
        user = _user(db)
        run = _seed_run(db, user.id, candidates=[
            {"symbol": "AAPL", "source": "screen_index", "status": "shortlisted", "scores": {"composite": 0.85}},
            {"symbol": "IWDA", "source": "screen_etf", "status": "shortlisted", "scores": {"composite": 0.8}},
        ])
        for symbol, rank in (("AAPL", 3), ("IWDA", None)):
            db.add(DiscoverCandidateSnapshot(
                user_id=user.id, run_id=run.id, issued_at=datetime.now(UTC), issue_date=datetime.now(UTC).date(),
                symbol=symbol, instrument_group="stock" if rank else "etf", evaluable=True, composite=0.8,
                components_json={}, stock_rank=rank, n_evaluable_stocks=187, shortlisted=True,
                sector_capped=False, picked=True, cohort_id="c1-x", provenance_json={},
            ))
        db.commit()

        by_symbol = {c.symbol: c for c in get_run(run.id, db=db, user=user).candidates}

        assert (by_symbol["AAPL"].pool_rank, by_symbol["AAPL"].pool_size) == (3, 187)
        assert by_symbol["IWDA"].pool_rank is None and by_symbol["IWDA"].pool_size is None


class TestApiListRuns:
    """Integration: GET /api/discover/runs filters by owner."""

    def test_list_runs_only_shows_owner_runs(self):
        db = _memory_db()
        alice = _user(db, "alice")
        bob = _user(db, "bob")

        _seed_run(db, alice.id, status="completed")
        _seed_run(db, alice.id, status="running")
        _seed_run(db, bob.id, status="completed")

        alice_runs = list_runs(db=db, user=alice)
        bob_runs = list_runs(db=db, user=bob)

        assert len(alice_runs) == 2
        assert len(bob_runs) == 1


# ======================================================================
# Section 2 — Edge cases
# ======================================================================


def _consume_pipeline(generator):
    try:
        while True:
            next(generator)
    except StopIteration as exc:
        return exc.value


class TestEdgeCases:
    """Graceful behaviour under degraded or edge conditions."""

    def test_empty_candidates_returns_empty_shortlist(self):
        db = _memory_db()
        with patch(
            "app.decision.discover.pipeline.MacroRegimeGate"
        ) as mock_gate_cls:
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate
            with patch("app.decision.discover.pipeline._preingest_candidates"):
                gen = run_candidate_pipeline(db, "user1", [], profile={})
                result = _consume_pipeline(gen)

        assert result == []

    def test_all_candidates_rejected_yields_empty_shortlist(self):
        db = _memory_db()
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate

            # Every stage rejects the candidate
            for stage in (
                "stage_history_ingest",
                "stage_alpha_miner",
            ):
                stack.enter_context(
                    patch(
                        f"app.decision.discover.pipeline.{stage}",
                        return_value=({}, "no data"),
                    )
                )

            gen = run_candidate_pipeline(
                db, "user1", [{"symbol": "DOOMED", "source": "screen_index"}],
            )
            result = _consume_pipeline(gen)

        assert result == []

    def test_cancel_token_blocks_processing(self):
        """RaiseIfCancelled token stops the generator mid-pipeline."""
        db = _memory_db()
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate

            cancel_token = MagicMock()
            cancel_token.raise_if_cancelled.side_effect = Exception("cancelled")

            gen = run_candidate_pipeline(
                db,
                "user1",
                [{"symbol": "AAPL", "source": "screen_index"}],
                cancel_token=cancel_token,
            )

            with pytest.raises(Exception, match="cancelled"):
                next(gen)

    def test_null_dossier_json_becomes_empty_defaults(self):
        db = _memory_db()
        user = _user(db)
        dossier = _seed_dossier(db, dossier_json={})

        run = DiscoverRun(id=uuid4().hex, user_id=user.id, status="completed")
        db.add(run)
        db.commit()
        db.add(
            DiscoverCandidate(
                id=uuid4().hex,
                run_id=run.id,
                symbol="EMPTY",
                source="screen_index",
                status="shortlisted",
                dossier_id=dossier.id,
            )
        )
        db.commit()

        result = get_dossier(dossier.id, db=db, user=user)

        assert result.thesis == ""
        assert result.direction == "neutral"
        assert result.horizon_months == 6
        assert result.key_risks == []

    def test_candidate_with_null_scores_json_handled(self):
        db = _memory_db()
        user = _user(db)
        run = _seed_run(
            db,
            user.id,
            candidates=[
                {
                    "symbol": "WEIRD",
                    "source": "screen_index",
                    "status": "shortlisted",
                    "scores": None,  # Force None
                }
            ],
        )
        cand = db.query(DiscoverCandidate).filter_by(run_id=run.id).first()
        assert cand is not None
        cand.scores_json = ""
        db.commit()

        result = get_run(run.id, db=db, user=user)
        assert result.candidates[0].scores == {}

    def test_candidate_with_corrupt_scores_json_handled(self):
        db = _memory_db()
        user = _user(db)
        run = _seed_run(
            db,
            user.id,
            candidates=[
                {
                    "symbol": "BROKEN",
                    "source": "screen_index",
                    "status": "shortlisted",
                    "scores": {},
                }
            ],
        )
        cand = db.query(DiscoverCandidate).filter_by(run_id=run.id).first()
        assert cand is not None
        cand.scores_json = "not valid json {{{"
        db.commit()

        result = get_run(run.id, db=db, user=user)
        assert result.candidates[0].scores == {}

    def test_degradation_with_zero_providers(self):
        """All-providers-fail → degradation_score=1.0, all fallbacks used."""
        db = _memory_db()

        class _NoProvider:
            def __init__(self, name: str):
                self.name = name
                self.enabled = True

        class EmptyRegistry:
            providers: list = []

            def get_quote(self, symbol: str) -> dict:
                return {"ok": False, "error": "no providers"}

        with patch(
            "app.decision.discover.provider_degradation.build_provider_registry",
            return_value=EmptyRegistry(),
        ):
            result = DegradationAssessor().assess("AAPL", db)

        assert result.symbol == "AAPL"
        assert result.degradation_score == pytest.approx(1.0)
        assert result.fallbacks_used == 0
        assert result.warnings

    def test_regime_gate_blocks_pipeline_via_orchestrator_flow(self):
        """Integration: Gate blocks → shortlist is empty list, not None or error."""
        db = _memory_db()
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = True
            mock_gate.context_dict.return_value = {
                "regime_label": "bear",
                "allowed": False,
            }
            mock_gate_cls.return_value = mock_gate

            gen = run_candidate_pipeline(
                db, "user1", [{"symbol": "AAPL", "source": "screen_index"}],
            )
            result = _consume_pipeline(gen)

        assert result == []


# ======================================================================
# Section 3 — Cross-component data flow
# ======================================================================


class TestCrossComponentDataFlow:
    """End-to-end: dossier writes → API surfaces, composite scoring, fail-open."""

    def test_dossier_writer_persists_structured_json(self, monkeypatch):
        """DossierWriter writes structured §5 JSON and persists it on the dossier row."""
        db = _memory_db()
        user = _user(db)

        monkeypatch.setattr(
            "app.decision.discover.dossier_writer._local_llm_sync",
            lambda _db, _messages, timeout_s=120.0, **_kw: json.dumps(
                {
                    "direction": "long",
                    "conviction": 0.75,
                    "horizon_months": 6,
                    "thesis": "Buy AAPL on AI momentum",
                    "key_risks": ["Valuation risk"],
                }
            ),
        )
        monkeypatch.setattr(
            "app.foundation.market.fundamentals",
            lambda _db, _symbol: {
                "data": {"pe_ratio": 28.0, "pb_ratio": 12.0, "roe": 0.45},
                "source": "mock",
                "stale": False,
            },
        )
        monkeypatch.setattr(
            "app.decision.discover.dossier_writer._fetch_sentiment",
            lambda _symbol, _db: {
                "news_sentiment": 0.75,
                "social_mentions": 12,
                "sentiment_trend": "rising",
            },
        )

        candidate = {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "isin": "US0378331005",
            "source": "screen_index",
            "scores_json": json.dumps(
                {"composite": 0.85, "backtest_vs_benchmark": {"candidate_return_annual": 0.20}}
            ),
            "tradeable_json": json.dumps(
                {"teilfreistellung_class": "aktien", "domicile": "US"}
            ),
        }
        write_result = write_dossier(db, candidate, {"user_id": user.id})

        dossier = db.get(RecommendationDossier, write_result["dossier_id"])
        assert dossier is not None
        data = json.loads(dossier.dossier_json)

        assert data["direction"] == "long"
        assert data["conviction"] == 0.85  # pipeline composite overwrites LLM conviction
        assert data["horizon_months"] == 6
        # expected_return is never taken from the LLM (12.5 above) — always
        # derived deterministically from the quant anchor. ADR 0017: prior
        # 2.2% cash + 1.0 x 4.5% = 6.7%, plus 0.10 of the gap to the 20.0%/yr
        # anchor, at horizon 6mo -> (6.7 + 1.33) * 0.5 = 4.0
        assert data["expected_return"] == pytest.approx(4.0, abs=0.05)
        assert data["thesis"] == "Buy AAPL on AI momentum"
        assert data["key_risks"] == ["Valuation risk"]
        assert data["signal_breakdown"]["composite_score"] == 0.85
        assert data["estimate"] is True
        assert data["not_financial_advice"] is True
        assert dossier.conviction > 0.0

    def test_composite_scoring_with_missing_stages_is_neutral(self):
        """A candidate that passes only some stages gets a neutral composite, not crash."""
        db = _memory_db()
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate

            # Only history_ingest and verification_gate succeed; alpha_miner/screener,
            # sentiment_fundamentals, momentum_quality, backtest, portfolio_fit all
            # complete but yield minimal neutral scores.
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_history_ingest",
                    return_value=({"concerns": []}, None),
                )
            )
            # Minimal scores → composite should be in the 0.3-0.5 range (neutral)
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_alpha_miner",
                    return_value=({"ic": 0.0, "icir": 0.0, "concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_alpha_screener",
                    return_value=({"regime_affinity": 0.5, "concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_sentiment_fundamentals",
                    return_value=(
                        {"analyst_estimate_score": 0.5, "sentiment_score": 0.5,
                         "fundamentals_score": 0.5, "concerns": []},
                        None,
                    ),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_momentum_quality",
                    return_value=({"concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_backtest_vs_benchmark",
                    return_value=({"concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_verification_gate",
                    return_value=({"sharpe": 0.0, "volatility": 0.3, "concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_quant_signals",
                    return_value=({"cvar_95_daily": -0.02, "concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_portfolio_fit",
                    return_value=({"fit_score": 0.5, "concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_estimate_revision_signal",
                    return_value=({"sue": None, "revision_momentum": None, "concerns": []}, None),
                )
            )
            stack.enter_context(
                patch(
                    "app.decision.discover.pipeline.stage_insider_signal",
                    return_value=({"cluster_buy_score": None, "net_insider_flow_usd": None, "concerns": []}, None),
                )
            )

            gen = run_candidate_pipeline(
                db, "user1", [{"symbol": "NEUTRAL", "source": "screen_index"}],
            )
            result = _consume_pipeline(gen)

        assert len(result) == 1
        assert result[0]["symbol"] == "NEUTRAL"
        assert result[0]["reject_stage"] is None
        # Neutral inputs → mid-range composite, no crash.
        assert 0.3 <= result[0]["composite_score"] <= 0.65

    def _seed_prices(self, db, ticker: str, closes: list[float]) -> None:
        """Seed PriceCache rows for a ticker with business-day dates."""
        from datetime import date, timedelta

        from app.foundation.models.entities import PriceCache

        end = date.today()
        days: list[date] = []
        d = end
        while len(days) < len(closes):
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        days.reverse()
        now = datetime.now(UTC)
        for dd, close in zip(days, closes):
            db.add(
                PriceCache(
                    id=uuid4().hex, ticker=ticker.upper(), date=dd,
                    close=Decimal(str(round(close, 4))), fetched_at=now,
                    source="test", stale=False, currency="EUR",
                )
            )
        db.commit()

    def test_portfolio_fit_with_no_candidate_price_history(self):
        """No price history for candidate → rejected with data-availability message."""
        db = _memory_db()
        user = _user(db)

        self._seed_prices(db, "PORT", [50.0 + i * 0.1 for i in range(200)])

        from app.foundation.models.entities import DkbAccount, DkbPosition
        account = DkbAccount(
            id=uuid4().hex, user_id=user.id, type="depot",
            iban="DE" + uuid4().hex[:20], balance=Decimal("0"), currency="EUR",
        )
        db.add(account)
        db.commit()
        db.add(
            DkbPosition(
                id=uuid4().hex, account_id=account.id, isin="X",
                ticker="PORT", name="Port", quantity=Decimal("1"),
                avg_buy_price=Decimal("50"), current_price=Decimal("60"),
                current_value=Decimal("60"),
            )
        )
        db.commit()

        from app.decision.discover.pipeline import stage_portfolio_fit

        with patch(
            "app.decision.discover.pipeline.market_history",
            return_value=[],
        ):
            scores, reject = stage_portfolio_fit(db, user.id, "NODATA")

        assert reject == "portfolio_fit: no candidate price data"
        assert scores is None

    def test_composite_score_with_strong_forward_looking_signals(self):
        """Strong quant + AlphaCrafter + analyst signals → high composite."""
        db = _memory_db()
        scores = {
            "alpha_miner": {"ic": 0.15, "icir": 1.2, "n_obs": 100, "concerns": []},
            "alpha_screener": {"regime_affinity": 0.92, "concerns": []},
            "sentiment_fundamentals": {
                "analyst_estimate_score": 0.9,
                "sentiment_score": 0.75,
                "fundamentals_score": 0.8,
                "concerns": [],
            },
            "momentum_quality": {"momentum_12_1m": 0.35, "volatility_6m": 0.18, "concerns": []},
            "backtest_vs_benchmark": {"excess_return_annual": 0.12, "sharpe_delta": 0.6, "concerns": []},
            "verification_gate": {"sharpe": 1.5, "volatility": 0.18, "max_drawdown": 0.08, "concerns": []},
            "quant_signals": {"cvar_95_daily": -0.012, "concerns": []},
            "portfolio_fit": {"fit_score": 0.85, "concerns": []},
        }

        ic = 0.15
        icir = 1.2
        ic_score = max(0.0, min(1.0, 0.5 + ic * 3.0 + icir * 0.25))
        # 0.5 + 0.45 + 0.3 = 1.25 → cap at 1.0
        assert ic_score == pytest.approx(1.0)

        with ExitStack() as stack:
            stack.enter_context(
                patch("app.decision.discover.pipeline._preingest_candidates")
            )
            mock_gate_cls = stack.enter_context(
                patch("app.decision.discover.pipeline.MacroRegimeGate")
            )
            mock_gate = MagicMock()
            mock_gate.blocks_discovery.return_value = False
            mock_gate_cls.return_value = mock_gate

            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_history_ingest",
                      return_value=({"concerns": []}, None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_alpha_miner",
                      return_value=(scores["alpha_miner"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_alpha_screener",
                      return_value=(scores["alpha_screener"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_sentiment_fundamentals",
                      return_value=(scores["sentiment_fundamentals"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_momentum_quality",
                      return_value=(scores["momentum_quality"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_backtest_vs_benchmark",
                      return_value=(scores["backtest_vs_benchmark"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_verification_gate",
                      return_value=(scores["verification_gate"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_quant_signals",
                      return_value=(scores["quant_signals"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_portfolio_fit",
                      return_value=(scores["portfolio_fit"], None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_estimate_revision_signal",
                      return_value=({"sue": None, "revision_momentum": None, "concerns": []}, None))
            )
            stack.enter_context(
                patch("app.decision.discover.pipeline.stage_insider_signal",
                      return_value=({"cluster_buy_score": None, "net_insider_flow_usd": None, "concerns": []}, None))
            )

            gen = run_candidate_pipeline(
                db, "user1", [{"symbol": "STRONG", "source": "screen_index"}],
            )
            result = _consume_pipeline(gen)

        assert len(result) == 1
        assert result[0]["symbol"] == "STRONG"
        # Strong quant + advisory signals → high composite
        assert result[0]["composite_score"] >= 0.75

    def test_preingest_empty_symbols_is_noop(self):
        """_preingest_candidates with empty list → no call to _preingest_one."""
        called = False

        def _mock_preingest_one(sym):
            nonlocal called
            called = True
            return (sym, True)

        with patch(
            "app.decision.discover.pipeline._preingest_one", _mock_preingest_one
        ):
            _preingest_candidates([])

        assert not called


# ======================================================================
# Section 4 — Utility + safety
# ======================================================================


class TestSafeJsonLoads:
    def test_valid_json(self):
        assert _safe_json_loads('{"key": "value"}') == {"key": "value"}

    def test_none_input(self):
        assert _safe_json_loads(None) == {}

    def test_empty_string(self):
        assert _safe_json_loads("") == {}

    def test_corrupt_json(self):
        assert _safe_json_loads("not json {{{") == {}

    def test_json_array(self):
        """Non-dict JSON returns empty dict (guard in _safe_json_loads)."""
        assert _safe_json_loads("[1, 2, 3]") == {}

    def test_int_value(self):
        """Non-string-like input returns empty dict."""
        # The function signature expects str|None, but we test defensive handling
        assert _safe_json_loads(42) == {}  # type: ignore[arg-type]
