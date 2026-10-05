"""Regression tests for the LLM mandate review fallback behaviour.

Covers the "always hold" root cause fixes:
1. An unreachable LLM endpoint must persist the decision with
   status="llm_unavailable" (not "completed") so the journal/UI can
   distinguish an outage from a genuine hold.
2. A tool-loop exhaustion without a decision must persist status="fallback_hold".
3. Fallback rows must be excluded from the journal block fed back to the LLM.
4. ISINs for tickers not currently held must resolve from the Asset table
   or Discover candidates (previously isin=None → TRADEABILITY gate rejected
   every new position).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    Asset,
    DiscoverCandidate,
    DiscoverRun,
    LlmPortfolioDecision,
    PaperPortfolio,
    User,
)


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # bar_prices is a raw hypertable not in Base.metadata
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS bar_prices ("
            "symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, "
            "close REAL, volume REAL, currency TEXT, provider TEXT)"
        ))
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> User:
    user = User(id=uuid4().hex, username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_unreachable_llm_persists_llm_unavailable_status(monkeypatch):
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(review_mod, "_local_llm_sync", _raise)

    result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "llm_unavailable"
    assert result["decision"]["action"] == "hold"
    assert result["trade_created"] is False

    row = db.query(LlmPortfolioDecision).one()
    assert row.status == "llm_unavailable"
    assert row.error is not None and "connection refused" in row.error
    decision = json.loads(row.decision_json)
    assert decision["action"] == "hold"
    assert "unreachable" in decision["thesis"]


def test_tool_loop_exhaustion_persists_fallback_hold(monkeypatch):
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    # LLM is reachable and always returns parseable, valid tool-call JSON,
    # but never commits to a decision → the turn budget exhausts.
    tool_call = json.dumps({"tool": "get_quote", "args": {"ticker": "SPY"}})
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: tool_call)

    result = review_mod.run_mandate_review(db, user.id, "B")

    assert result["status"] == "fallback_hold"
    assert result["decision"]["action"] == "hold"

    row = db.query(LlmPortfolioDecision).one()
    assert row.status == "fallback_hold"
    assert row.error is None


def test_persistent_unparseable_json_persists_review_failed(monkeypatch):
    """Unparseable JSON that survives the one grammar-constrained correction
    retry must fail fast as status="review_failed", distinct from
    fallback_hold (which means the model cooperated but ran out of turns).

    Previously this was indistinguishable from turn exhaustion: an
    unparseable completion just consumed a turn and nudged, silently
    reaching the same fallback_hold after burning the whole budget.
    """
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: "not json at all")

    result = review_mod.run_mandate_review(db, user.id, "B")

    assert result["status"] == "review_failed"
    assert result["decision"]["action"] == "hold"

    row = db.query(LlmPortfolioDecision).one()
    assert row.status == "review_failed"
    assert row.error is not None
    assert "not json at all" in row.error


def test_review_passes_grammar_schema_to_local_llm(monkeypatch):
    """The mandate loop must request grammar-constrained generation, the same
    defense advisor/llm_decision.py uses for its own decision step — prior to
    this, run_mandate_review's calls carried no json_schema at all, relying
    purely on prompting the model to reply with JSON."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    captured_kwargs: list[dict] = []

    def _fake(db_arg, messages, **kwargs):
        captured_kwargs.append(kwargs)
        return json.dumps({
            "decision": {
                "action": "hold", "ticker": "", "quantity": 0,
                "thesis": "Nothing actionable this week.",
                "expected_outcome": "e", "confidence": 0.5,
            }
        })

    monkeypatch.setattr(review_mod, "_local_llm_sync", _fake)

    result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    assert captured_kwargs, "expected at least one _local_llm_sync call"
    assert captured_kwargs[0]["json_schema"] is review_mod._MANDATE_RESPONSE_SCHEMA
    assert captured_kwargs[0]["sampling"] == review_mod.STRUCTURED_JSON_SAMPLING


def test_review_parses_markdown_fenced_decision(monkeypatch):
    """A completion wrapped in ``` fences must still parse — a very common
    model habit that the grammar constraint doesn't eliminate on its own."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    fenced = (
        "```json\n"
        + json.dumps({
            "decision": {
                "action": "hold", "ticker": "", "quantity": 0,
                "thesis": "Fenced but valid.", "expected_outcome": "e", "confidence": 0.5,
            }
        })
        + "\n```"
    )
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: fenced)

    result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    assert result["decision"]["thesis"] == "Fenced but valid."


def test_review_parses_json_embedded_in_prose_preamble(monkeypatch):
    """A completion with a leading sentence before the JSON object must still
    parse — the old fence-only stripper had no recovery for this shape at
    all, so it would nudge and consume a turn every time it occurred."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    prose_wrapped = "Here is my decision: " + json.dumps({
        "decision": {
            "action": "hold", "ticker": "", "quantity": 0,
            "thesis": "Embedded in prose.", "expected_outcome": "e", "confidence": 0.5,
        }
    })
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: prose_wrapped)

    result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    assert result["decision"]["thesis"] == "Embedded in prose."


def test_review_recovers_after_one_json_correction_retry(monkeypatch):
    """One unparseable turn followed by a valid one must still complete —
    only a *second consecutive* failure gives up on the review."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    responses = iter([
        "not json at all",
        json.dumps({
            "decision": {
                "action": "hold", "ticker": "", "quantity": 0,
                "thesis": "Recovered after correction.", "expected_outcome": "e", "confidence": 0.5,
            }
        }),
    ])
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: next(responses))

    result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    assert result["decision"]["thesis"] == "Recovered after correction."


def test_non_schema_action_persists_invalid_action_status(monkeypatch):
    """An LLM decision with action="rebalance" must not pass as "completed".

    The executor only knows buy/sell/hold; previously a non-schema action
    persisted as a completed review that silently created no trade. Now the
    LLM is nudged to correct it, and if it never does, the row is persisted
    with status="invalid_action", coerced to hold, thesis preserved.
    """
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    rebalance = json.dumps({
        "decision": {
            "action": "rebalance",
            "ticker": "EOAN.DE",
            "quantity": 10,
            "thesis": "Drift exceeds mandate threshold.",
            "expected_outcome": "Restore target weights.",
            "confidence": 0.6,
        }
    })
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: rebalance)

    result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "invalid_action"
    assert result["decision"]["action"] == "hold"
    assert result["trade_created"] is False

    row = db.query(LlmPortfolioDecision).one()
    assert row.status == "invalid_action"
    assert row.error is not None and "rebalance" in row.error
    decision = json.loads(row.decision_json)
    assert decision["action"] == "hold"
    assert decision["thesis"] == "Drift exceeds mandate threshold."


def test_non_schema_action_corrected_after_nudge(monkeypatch):
    """If the LLM corrects to a valid action after the nudge, the review
    completes normally with the corrected decision."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    responses = iter([
        json.dumps({"decision": {"action": "rebalance", "ticker": "SPY", "quantity": 1,
                                 "thesis": "t", "expected_outcome": "e", "confidence": 0.5}}),
        json.dumps({"decision": {"action": "hold", "ticker": "", "quantity": 0,
                                 "thesis": "corrected", "expected_outcome": "e", "confidence": 0.5}}),
    ])
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: next(responses))

    result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    assert result["decision"]["action"] == "hold"
    assert result["decision"]["thesis"] == "corrected"

    row = db.query(LlmPortfolioDecision).one()
    assert row.status == "completed"
    assert row.error is None


def test_journal_block_excludes_fallback_rows():
    from app.decision.llm_portfolio.context import _build_journal_block

    db = _memory_db()
    user = _user(db)
    portfolio = PaperPortfolio(id=uuid4().hex, user_id=user.id, name="P", mandate="A")
    db.add(portfolio)
    db.commit()

    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    for status in ("completed", "llm_unavailable", "fallback_hold", "review_failed"):
        db.add(LlmPortfolioDecision(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            mandate="A",
            review_date=now,
            decision_json=json.dumps({"action": "hold", "thesis": status}),
            status=status,
        ))
    db.commit()

    entries = _build_journal_block(db, portfolio.id)
    assert len(entries) == 1


def test_regime_block_missing_table_keeps_session_usable():
    """A missing market_regimes table must not poison the session.

    On Postgres a failed statement aborts the whole transaction; the regime
    block runs in a SAVEPOINT so later queries (LLM settings lookup, decision
    INSERT) still work. market_regimes is not in Base.metadata, so it is
    genuinely absent here.
    """
    from app.decision.llm_portfolio.context import _build_regime_block

    db = _memory_db()
    user = _user(db)
    portfolio = PaperPortfolio(id=uuid4().hex, user_id=user.id, name="P", mandate="A")
    db.add(portfolio)
    db.commit()

    block = _build_regime_block(db, portfolio.id)
    assert block == {"note": "Regime data unavailable"}

    # Session must still accept queries after the swallowed failure.
    assert db.query(PaperPortfolio).count() == 1


def test_resolve_isin_from_asset_and_discover():
    from app.decision.llm_portfolio.review import _resolve_isin

    db = _memory_db()
    user = _user(db)

    db.add(Asset(isin="US78462F1030", symbol="SPY", name="SPDR S&P 500"))
    db.commit()
    assert _resolve_isin(db, "SPY") == "US78462F1030"

    run = DiscoverRun(id=uuid4().hex, user_id=user.id)
    db.add(run)
    db.flush()
    db.add(DiscoverCandidate(
        id=uuid4().hex,
        run_id=run.id,
        symbol="ASML",
        isin="NL0010273215",
        source="test",
        status="shortlisted",
    ))
    db.commit()
    assert _resolve_isin(db, "ASML") == "NL0010273215"

    assert _resolve_isin(db, "UNKNOWN") is None


def _link_telegram(db, user) -> None:
    """Link a fake Telegram account (chat_id "42") to ``user``."""
    from app.foundation.telegram_bot import generate_pairing_code, link_pairing_code

    pairing = generate_pairing_code(db, user.id)
    link_pairing_code(db, pairing.code, "42")


def test_review_sends_telegram_digest_when_linked(monkeypatch):
    """A completed review with a linked Telegram account sends one digest
    message via the existing leaf-channel-adapter (telegram_bot.send_telegram_message)."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)
    _link_telegram(db, user)

    hold = json.dumps({
        "decision": {
            "action": "hold",
            "ticker": "",
            "quantity": 0,
            "thesis": "Nothing actionable this week.",
            "expected_outcome": "Maintain current allocation.",
            "confidence": 0.5,
        }
    })
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: hold)

    with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
        result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    mock_send.assert_called_once()
    chat_id, text_sent = mock_send.call_args[0][1], mock_send.call_args[0][2]
    assert chat_id == "42"
    assert "Mandate A review complete" in text_sent
    assert "HOLD" in text_sent


def test_review_no_telegram_send_when_unlinked(monkeypatch):
    """No linked Telegram account → no send attempt, review still completes."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)

    hold = json.dumps({
        "decision": {
            "action": "hold", "ticker": "", "quantity": 0,
            "thesis": "t", "expected_outcome": "e", "confidence": 0.5,
        }
    })
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: hold)

    with patch("app.foundation.telegram_bot.send_telegram_message", return_value=True) as mock_send:
        result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    mock_send.assert_not_called()


def test_review_survives_telegram_send_failure(monkeypatch):
    """A raising Telegram send must not fail the review (broad try/except)."""
    from app.decision.llm_portfolio import review as review_mod

    db = _memory_db()
    user = _user(db)
    _link_telegram(db, user)

    hold = json.dumps({
        "decision": {
            "action": "hold", "ticker": "", "quantity": 0,
            "thesis": "t", "expected_outcome": "e", "confidence": 0.5,
        }
    })
    monkeypatch.setattr(review_mod, "_local_llm_sync", lambda *a, **k: hold)

    def _raise(*args, **kwargs):
        raise RuntimeError("telegram is down")

    with patch("app.foundation.telegram_bot.send_telegram_message", side_effect=_raise):
        result = review_mod.run_mandate_review(db, user.id, "A")

    assert result["status"] == "completed"
    row = db.query(LlmPortfolioDecision).one()
    assert row.status == "completed"


def test_format_mandate_review_digest_buy_and_gates():
    from app.foundation.telegram_replies import format_mandate_review_digest

    decision = {"action": "buy", "ticker": "IWDA.L", "thesis": "Strong momentum and cheap valuation." * 10}
    gate_result = {"passed": True, "checks": [], "reason": "ok"}
    text_out = format_mandate_review_digest("A", decision, gate_result, True, "completed")

    assert "Mandate A review complete (completed)" in text_out
    assert "Action: BUY IWDA.L" in text_out
    assert "Gates: passed" in text_out
    assert "Thesis:" in text_out
    # Truncated to the configured max, plus ellipsis.
    thesis_line = [ln for ln in text_out.splitlines() if ln.startswith("Thesis:")][0]
    assert len(thesis_line) <= len("Thesis: ") + 240 + 3


def test_format_mandate_review_digest_rejected_buy_shows_reason():
    from app.foundation.telegram_replies import format_mandate_review_digest

    decision = {"action": "buy", "ticker": "HEN3.DE", "thesis": "Concentration breach candidate."}
    gate_result = {"passed": False, "checks": [], "reason": "TRADEABILITY: unresolved ISIN"}
    text_out = format_mandate_review_digest("B", decision, gate_result, False, "completed")

    assert "Action: BUY HEN3.DE (not executed)" in text_out
    assert "Gates: failed — TRADEABILITY: unresolved ISIN" in text_out


def test_format_mandate_review_digest_hold():
    from app.foundation.telegram_replies import format_mandate_review_digest

    decision = {"action": "hold", "ticker": "", "thesis": ""}
    text_out = format_mandate_review_digest("A", decision, None, False, "fallback_hold")

    assert "Action: HOLD (no trade)" in text_out
    assert "Gates: n/a" in text_out
    assert "Thesis:" not in text_out
