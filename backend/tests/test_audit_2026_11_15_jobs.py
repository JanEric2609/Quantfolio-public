"""Audit 2026-11-15: the scheduled jobs report failure, and the router stops abandoned requests."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from conftest import _memory_db
from sqlalchemy.orm import sessionmaker

from app.foundation.models.entities import LlmPortfolioDecision, User


@pytest.fixture
def db_factory(monkeypatch):
    """Every ``SessionLocal()`` in the job code opens a session on one in-memory database."""
    db = _memory_db()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
    monkeypatch.setattr("app.foundation.core.db.SessionLocal", factory)
    db.add(User(username="jobs", password_hash="x"))
    db.commit()
    return db


def _capture(monkeypatch, module) -> dict[str, Any]:
    """Replace the module's ``register_cron_job`` with one that keeps the job functions."""
    jobs: dict[str, Any] = {}

    def register(job_id, fn, **_kwargs):
        jobs[job_id] = fn
        return job_id

    monkeypatch.setattr(module, "register_cron_job", register)
    return jobs


def test_a_failed_mandate_review_ends_failed_and_is_journalled(monkeypatch, db_factory):
    from app.decision.llm_portfolio import jobs as llm_jobs
    from app.decision.llm_portfolio import review

    def boom(db, user_id, mandate):
        raise RuntimeError("MultipleResultsFound")

    monkeypatch.setattr(review, "run_mandate_review", boom)
    jobs = _capture(monkeypatch, llm_jobs)
    llm_jobs.register_llm_portfolio_review_jobs(None)
    out = jobs["llm_mandate_b_review"]()
    assert out["status"] == "failed" and out["failed"] == 1
    assert "MultipleResultsFound" in out["reason"]
    row = db_factory.query(LlmPortfolioDecision).one()
    assert row.status == "review_failed" and row.mandate == "B"


def test_a_successful_mandate_review_ends_ok(monkeypatch, db_factory):
    from app.decision.llm_portfolio import jobs as llm_jobs
    from app.decision.llm_portfolio import review

    monkeypatch.setattr(review, "run_mandate_review", lambda db, user_id, mandate: {"status": "completed"})
    jobs = _capture(monkeypatch, llm_jobs)
    llm_jobs.register_llm_portfolio_review_jobs(None)
    assert jobs["llm_mandate_a_review"]() == {"status": "ok", "reviewed": 1}


def test_a_failed_ladder_run_is_reported(monkeypatch, db_factory):
    from app.decision.graduation import jobs as grad_jobs
    from app.decision.graduation import ladder

    def boom(db, user_id):
        raise ValueError("no NAV")

    monkeypatch.setattr(ladder, "run_ladder", boom)
    jobs = _capture(monkeypatch, grad_jobs)
    grad_jobs.register_ladder_evidence_job(None)
    out = jobs["ladder_evidence"]()
    assert out["status"] == "failed" and "no NAV" in out["reason"]


def test_the_weekly_trust_ledger_reports_its_failed_steps(monkeypatch, db_factory):
    from app.decision.verification import candidate_outcomes, daily_ledger, daily_tests
    from app.decision.verification import jobs as ver_jobs

    def boom(db, refresh=True):
        raise RuntimeError("prices down")

    def gate_boom(db, users):
        raise RuntimeError("gate down")

    monkeypatch.setattr(candidate_outcomes, "resolve_candidate_outcomes", boom)
    monkeypatch.setattr(daily_ledger, "ledger_users", lambda db: [])
    monkeypatch.setattr(daily_tests, "store_buy_gate_evidence", gate_boom)
    jobs = _capture(monkeypatch, ver_jobs)
    ver_jobs.register_trust_weekly_ledger_job(None)
    out = jobs["trust_weekly_ledger"]()
    assert out["status"] == "degraded"
    assert "candidate outcomes" in out["reason"] and "BUY gate evidence" in out["reason"]


def test_the_universe_prefetch_reports_an_outage(monkeypatch, db_factory):
    from app.decision.discover import jobs as discover_jobs

    monkeypatch.setattr(
        discover_jobs, "discover_universe_prefetch_inner", lambda db: {"symbols": 10, "bars_failed": 9},
    )
    jobs = _capture(monkeypatch, discover_jobs)
    discover_jobs.register_discover_universe_prefetch_job(None)
    assert jobs["discover_universe_prefetch"]()["status"] == "degraded"


# --- 2.24 walk-forward weights -------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["walkforward", "combinatorial"])
def test_cross_validated_weights_are_not_zero(scheme):
    import numpy as np
    import pandas as pd

    from app.foundation.quant_optim import run_optimisation

    rng = np.random.default_rng(1)
    days = pd.bdate_range("2023-01-02", periods=600)
    matrix = {
        t: dict(zip((d.date().isoformat() for d in days), 100 * np.exp(np.cumsum(rng.normal(3e-4, 0.01, 600)))))
        for t in "ABC"
    }
    out = run_optimisation(matrix, objective="min_risk", risk_measure="variance", cv_scheme=scheme, lookback_days=2000)
    assert out["status"] == "completed"
    assert sum(out["weights"].values()) == pytest.approx(1.0, abs=1e-6)
    assert all(w > 0 for w in out["weights"].values())
    assert out["cv_summary"]["annualized_return"] is not None


# --- 2.7 Mandate B book -------------------------------------------------------------------------------


def test_a_holding_without_a_fresh_quote_is_marked_unquoted():
    from datetime import UTC, datetime, timedelta

    from app.decision.llm_portfolio.rules import _book
    from app.foundation.models.entities import PaperHolding, PaperPortfolio

    db = _memory_db()
    user = User(username="b", password_hash="x")
    db.add(user)
    db.commit()
    p = PaperPortfolio(user_id=user.id, name="B", initial_cash=Decimal("100"), baseline_value=Decimal("100"),
                       mandate="B", managed_by="llm", inception_at=datetime.now(UTC) - timedelta(days=3))
    db.add(p)
    db.commit()
    for ticker in ("FRESH", "STALE", "NONE"):
        db.add(PaperHolding(portfolio_id=p.id, ticker=ticker, name=ticker, quantity=Decimal("1"),
                            avg_buy_price=Decimal("10"), currency="EUR"))
    db.commit()

    class Paper:
        @staticmethod
        def paper_quote_eur(db, ticker):
            return {"FRESH": {"price": 12.0}, "STALE": {"price": 11.0, "stale": True}}.get(ticker, {"price": None})

    unquoted: set[str] = set()
    book, _cash = _book(db, p, Paper, unquoted)
    assert unquoted == {"STALE", "NONE"}
    assert book["FRESH"][0] == 12.0 and book["STALE"][0] == 11.0 and book["NONE"][0] == 10.0


# --- 2.28 router ------------------------------------------------------------------------------------------


def test_an_abandoned_llm_request_is_cancelled_and_its_client_closed(monkeypatch):
    from app.foundation.llm import router
    from app.foundation.llm.scheduler import get_scheduler

    state: dict[str, Any] = {"cancelled": False, "closed": False}

    class SlowBackend:
        async def complete(self, messages, **kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def aclose(self):
            state["closed"] = True

    class Config:
        def __init__(self, db):
            pass

        def get_backend_for_task(self, task_type, force_local):
            return SlowBackend(), False

    monkeypatch.setattr(router, "RouterConfig", Config)
    monkeypatch.setattr(router, "_audit", lambda *a, **k: None)

    async def run() -> None:
        with pytest.raises(router.QueueWaitTimeoutError):
            await router.call(None, "routine", [{"role": "user", "content": "x"}], timeout_s=0.05)
        for _ in range(50):
            if state["closed"]:
                break
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert state["cancelled"] and state["closed"]
    assert get_scheduler().pending == {}


# --- 2.30 Telegram -----------------------------------------------------------------------------------------


def test_a_telegram_poll_timeout_is_no_error(monkeypatch, caplog):
    import httpx

    from app.foundation import telegram_bot

    class TimingOut:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(telegram_bot.httpx, "Client", TimingOut)
    with caplog.at_level("INFO", logger=telegram_bot.logger.name):
        assert telegram_bot._call_api("123:abc", "getUpdates", {}, timeout=1.0) is None
    assert all(r.levelname == "INFO" for r in caplog.records if "getUpdates" in r.getMessage())


# --- 2.4 advisor jobs ----------------------------------------------------------------------------------------


@pytest.fixture
def monday(monkeypatch):
    """The advisor jobs skip weekends; pin "now" to a Monday."""
    import datetime as dt

    real = dt.datetime

    class Monday(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 11, 16, 10, 0, tzinfo=tz)

    monkeypatch.setattr(dt, "datetime", Monday)


def test_the_advisor_cycle_reports_every_failed_step(monkeypatch, db_factory, monday):
    from app.decision.advisor import cycle, shadow, strategy
    from app.decision.advisor import jobs as advisor_jobs

    def boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(cycle, "backfill_paper_isins", boom)
    monkeypatch.setattr(strategy, "split_shared_challenger_sleeve", boom)
    monkeypatch.setattr(strategy, "get_champion", lambda db, user_id: object())
    monkeypatch.setattr(strategy, "get_active_challenger", lambda db, user_id: None)
    monkeypatch.setattr(cycle, "run_advisor_cycle", boom)
    monkeypatch.setattr(shadow, "run_shadow_forecasts", boom)
    jobs = _capture(monkeypatch, advisor_jobs)
    advisor_jobs.register_advisor_cycle_job(None)
    out = jobs["advisor_cycle"]()
    assert out["status"] == "failed"
    for step in ("ISIN backfill", "sleeve split", "champion", "shadow forecasts"):
        assert step in out["reason"]


def test_the_evolution_round_reports_a_failed_user(monkeypatch, db_factory, monday):
    from app.decision.advisor import evolution
    from app.decision.advisor import jobs as advisor_jobs

    monkeypatch.setattr(evolution, "run_evolution_round", lambda db, user_id: {"verdict": "keep"})
    jobs = _capture(monkeypatch, advisor_jobs)
    advisor_jobs.register_evolution_job(None)
    assert jobs["evolution_round"]()["status"] == "ok"

    def boom(db, user_id):
        raise RuntimeError("no scores")

    monkeypatch.setattr(evolution, "run_evolution_round", boom)
    assert jobs["evolution_round"]()["status"] == "failed"


def test_rl_training_reports_a_failed_user(monkeypatch, db_factory):
    from types import SimpleNamespace

    from app.decision.advisor import jobs as advisor_jobs
    from app.decision.advisor import rl_training

    def boom(db, user_id):
        raise RuntimeError("no env")

    monkeypatch.setattr("app.foundation.core.config.get_settings", lambda: SimpleNamespace(finrl_enabled=True))
    monkeypatch.setattr(rl_training, "train_and_validate_policy", boom)
    jobs = _capture(monkeypatch, advisor_jobs)
    advisor_jobs.register_advisor_rl_training_job(None)
    out = jobs["advisor_rl_training"]()
    assert out["status"] == "failed" and out["trained"] == 0
