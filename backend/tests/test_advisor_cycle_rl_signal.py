"""Tests for advisor/cycle.py's RL-signal wiring (Track D2): a validated
QuantRlPolicy's rollout weights reach the LLM prompt as an advisory
rl_suggested_weight, and a missing/broken policy row never blocks or
crashes the cycle (fails open, same discipline as every other gated
signal in this codebase)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
    DkbAccount,
    DkbPosition,
    PriceCache,
    QuantRlPolicy,
    User,
)
from app.decision.advisor.cycle import run_advisor_cycle


def _user(db, name="rl-cycle-user") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
    from app.foundation.models.entities import ListingCurrency

    # Test listings quote in EUR (the paper book values in EUR, and a ticker
    # without a suffix would otherwise be read as a USD listing).
    if db.get(ListingCurrency, ticker.upper()) is None:
        db.add(ListingCurrency(symbol=ticker.upper(), currency="EUR", source="test"))
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


def _seed_real_book(db, user: User, cash: float = 50_000.0) -> None:
    cash_account = DkbAccount(
        id=uuid4().hex, user_id=user.id, type="checking",
        iban="DE" + uuid4().hex[:20], balance=Decimal(str(cash)), currency="EUR",
    )
    depot = DkbAccount(
        id=uuid4().hex, user_id=user.id, type="depot",
        iban="DE" + uuid4().hex[:20], balance=Decimal("10000"), currency="EUR",
    )
    db.add_all([cash_account, depot])
    db.commit()
    db.add(
        DkbPosition(
            id=uuid4().hex, account_id=depot.id, isin="DE000TEST0001",
            ticker="HELD", name="Held Stock", quantity=Decimal("100"),
            avg_buy_price=Decimal("90"), current_price=Decimal("100"),
            current_value=Decimal("10000"),
        )
    )
    db.commit()


def _seed_discover_run(db, user: User, symbols: list[str]) -> DiscoverRun:
    run = DiscoverRun(id=uuid4().hex, user_id=user.id, status="completed", created_at=datetime.now(UTC))
    db.add(run)
    db.commit()
    for sym in symbols:
        db.add(DiscoverCandidate(
            id=uuid4().hex, run_id=run.id, symbol=sym, source="screen_index", status="shortlisted",
        ))
    db.commit()
    return run


def _wiggly_series(base: float, n: int = 300, drift_pct: float = 0.0004) -> list[float]:
    out = []
    for i in range(n):
        wiggle = base * (0.005 if i % 7 == 0 else (-0.004 if i % 5 == 0 else 0.0))
        out.append(base * (1 + i * drift_pct) + wiggle)
    return out


def _capturing_llm(decisions: list[dict], sink: list[dict]):
    def _call(db, messages):
        sink.append(json.loads(messages[1]["content"]))
        return json.dumps({"decisions": decisions})

    return _call


def _setup(db):
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly_series(90.0))
    _seed_prices(db, "CAND", _wiggly_series(40.0))
    return user


class TestAdvisorCycleRlSignal:
    def test_validated_policy_weights_reach_the_llm_prompt(self):
        db = _memory_db()
        user = _setup(db)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="completed",
            training_metrics_json=json.dumps({"final_weights": {"CAND": 0.33}}),
            finished_at=datetime.now(UTC),
        ))
        db.commit()

        seen_prompts: list[dict] = []
        result = run_advisor_cycle(
            db, user.id,
            llm_call=_capturing_llm(
                [{"ticker": "CAND", "action": "buy", "target_weight": 0.05,
                  "thesis": "t", "confidence": 0.6}],
                seen_prompts,
            ),
        )

        assert result["status"] == "completed"
        assert seen_prompts, "llm_call was never invoked"
        payload = seen_prompts[0]
        assert "rl_signal_note" in payload
        by_ticker = {c["ticker"]: c for c in payload["candidates"]}
        assert by_ticker["CAND"]["rl_suggested_weight"] == 0.33

    def test_no_policy_row_leaves_prompt_without_rl_fields(self):
        db = _memory_db()
        user = _setup(db)

        seen_prompts: list[dict] = []
        result = run_advisor_cycle(
            db, user.id,
            llm_call=_capturing_llm(
                [{"ticker": "CAND", "action": "buy", "target_weight": 0.05,
                  "thesis": "t", "confidence": 0.6}],
                seen_prompts,
            ),
        )

        assert result["status"] == "completed"
        payload = seen_prompts[0]
        assert "rl_signal_note" not in payload

    def test_unparsable_policy_metrics_fails_open_not_crash(self):
        """A QuantRlPolicy row with corrupted metrics must never break the cycle."""
        db = _memory_db()
        user = _setup(db)
        db.add(QuantRlPolicy(
            user_id=user.id, env_id="portfolio_allocation", algo="ppo",
            status="completed", training_metrics_json="not valid json",
            finished_at=datetime.now(UTC),
        ))
        db.commit()

        seen_prompts: list[dict] = []
        result = run_advisor_cycle(
            db, user.id,
            llm_call=_capturing_llm(
                [{"ticker": "CAND", "action": "buy", "target_weight": 0.05,
                  "thesis": "t", "confidence": 0.6}],
                seen_prompts,
            ),
        )

        assert result["status"] == "completed"
        payload = seen_prompts[0]
        assert "rl_signal_note" not in payload
