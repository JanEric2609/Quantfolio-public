"""Tests for the advisor loop (PR1 groups C + D).

Covers: trade-proposal building (C1), risk gate ceilings (C2), LLM decision
validation with a stubbed client (C3), real-book seeding idempotency (D0),
and the end-to-end cycle acceptance (D1/D2): ≥1 trade executed, ≥1 prediction
row written, snapshot count advanced. The LLM is always stubbed — never a
live model.
"""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverRun,
    DiscoveryPrediction,
    DkbAccount,
    DkbPosition,
    LlmPortfolioDecision,
    PaperHolding,
    PaperSnapshot,
    PriceCache,
    User,
)
from app.decision.advisor.cycle import ADVISOR_MANDATE, run_advisor_cycle
from app.decision.paper_portfolio import seed_paper_portfolio_from_real
from app.decision.advisor.decision import McSummary, TradeProposal, build_trade_proposal
from app.decision.advisor.llm_decision import decide_trades
from app.decision.advisor.risk_gate import DEFAULT_RISK_CEILINGS, check_risk_gate


def _user(db, name="jan") -> User:
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
    run = DiscoverRun(
        id=uuid4().hex, user_id=user.id, status="completed",
        created_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    for sym in symbols:
        db.add(
            DiscoverCandidate(
                id=uuid4().hex, run_id=run.id, symbol=sym, source="screen_index",
                status="shortlisted",
            )
        )
    db.commit()
    return run


def _wiggly_series(base: float, n: int = 300, drift_pct: float = 0.0004) -> list[float]:
    """Calm upward series (~0.5% daily wiggle) that stays inside risk ceilings."""
    out = []
    for i in range(n):
        wiggle = base * (0.005 if i % 7 == 0 else (-0.004 if i % 5 == 0 else 0.0))
        out.append(base * (1 + i * drift_pct) + wiggle)
    return out


def _stub_llm(decisions: list[dict]) -> callable:
    def _call(db, messages):
        return json.dumps({"decisions": decisions})

    return _call


# ---------------------------------------------------------------------------
# C2 — risk gate
# ---------------------------------------------------------------------------


def _proposal_with_envelope(envelope: dict) -> TradeProposal:
    return TradeProposal(
        portfolio_id="p1",
        suggested_weights={"AAA": 0.6, "BBB": 0.4},
        mc_summaries={
            "AAA": McSummary("AAA", "equity", -0.05, 0.01, 0.08, 0.6, 100.0),
            "BBB": McSummary("BBB", "equity", -0.15, 0.0, 0.12, 0.5, 50.0),
        },
        risk_envelope=envelope,
        current_book={},
        optimizer_status="completed",
    )


def test_risk_gate_blocks_cvar_breach():
    proposal = _proposal_with_envelope(
        {"var_95_daily": 0.02, "cvar_95_daily": 0.09, "max_drawdown": 0.10}
    )
    result = check_risk_gate(proposal)
    assert not result.passed
    assert any("cvar_95_daily" in b for b in result.breaches)
    assert result.passing_symbols == []
    assert result.blocked and all(b["reason"] for b in result.blocked)


def test_risk_gate_passes_within_limits():
    proposal = _proposal_with_envelope(
        {"var_95_daily": 0.01, "cvar_95_daily": 0.02, "max_drawdown": 0.10}
    )
    result = check_risk_gate(proposal)
    assert result.passed
    assert result.breaches == []
    assert set(result.passing_symbols) == {"AAA", "BBB"}


def test_risk_gate_default_ceilings_documented():
    assert DEFAULT_RISK_CEILINGS["cvar_95_daily"] > DEFAULT_RISK_CEILINGS["var_95_daily"]
    assert 0 < DEFAULT_RISK_CEILINGS["max_drawdown"] <= 0.5


# ---------------------------------------------------------------------------
# C3 — LLM decision wrapper (stubbed client)
# ---------------------------------------------------------------------------


def test_llm_decision_valid_structured_output():
    db = _memory_db()
    proposal = _proposal_with_envelope(
        {"var_95_daily": 0.01, "cvar_95_daily": 0.02, "max_drawdown": 0.10}
    )
    gate = check_risk_gate(proposal)
    decisions, errors = decide_trades(
        db, proposal, gate,
        llm_call=_stub_llm([
            {"ticker": "AAA", "action": "buy", "target_weight": 0.1,
             "thesis": "Positive MC skew.", "confidence": 0.7},
            {"ticker": "BBB", "action": "hold", "target_weight": 0.0,
             "thesis": "", "confidence": 0.5},
        ]),
    )
    assert errors == []
    assert len(decisions) == 2
    assert decisions[0].action == "buy"
    assert decisions[0].confidence == 0.7


def test_llm_decision_out_of_envelope_choice_rejected():
    db = _memory_db()
    proposal = _proposal_with_envelope(
        {"var_95_daily": 0.01, "cvar_95_daily": 0.02, "max_drawdown": 0.10}
    )
    gate = check_risk_gate(proposal)
    decisions, errors = decide_trades(
        db, proposal, gate,
        llm_call=_stub_llm([
            {"ticker": "EVIL", "action": "buy", "target_weight": 0.9,
             "thesis": "yolo", "confidence": 0.99},
        ]),
    )
    assert decisions == []
    assert any("outside risk-gate-passing set" in e for e in errors)


def test_llm_decision_invalid_action_rejected():
    db = _memory_db()
    proposal = _proposal_with_envelope(
        {"var_95_daily": 0.01, "cvar_95_daily": 0.02, "max_drawdown": 0.10}
    )
    gate = check_risk_gate(proposal)
    decisions, errors = decide_trades(
        db, proposal, gate,
        llm_call=_stub_llm([
            {"ticker": "AAA", "action": "maybe", "target_weight": 0.1,
             "thesis": "x", "confidence": 0.5},
        ]),
    )
    assert decisions == []
    assert any("invalid action" in e for e in errors)


def test_llm_decision_garbage_response():
    db = _memory_db()
    proposal = _proposal_with_envelope(
        {"var_95_daily": 0.01, "cvar_95_daily": 0.02, "max_drawdown": 0.10}
    )
    gate = check_risk_gate(proposal)
    decisions, errors = decide_trades(
        db, proposal, gate, llm_call=lambda db, m: "not json at all"
    )
    assert decisions == []
    assert errors


# ---------------------------------------------------------------------------
# D0 — seed from real book
# ---------------------------------------------------------------------------


def test_seed_paper_portfolio_clones_real_book_and_cash():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)

    portfolio = seed_paper_portfolio_from_real(db, user.id)
    assert portfolio.mandate == ADVISOR_MANDATE
    assert float(portfolio.initial_cash) == 50_000.0  # non-depot only

    holdings = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio.id).all()
    assert len(holdings) == 1
    assert holdings[0].ticker == "HELD"
    assert float(holdings[0].quantity) == 100.0

    basis = json.loads(portfolio.mandate_config_json)
    assert basis["seeded_from"] == "real_book"
    assert basis["cash_seeded"] == 50_000.0


def test_seed_is_idempotent():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)

    first = seed_paper_portfolio_from_real(db, user.id)
    holdings_before = db.query(PaperHolding).filter(PaperHolding.portfolio_id == first.id).count()
    second = seed_paper_portfolio_from_real(db, user.id)
    holdings_after = db.query(PaperHolding).filter(PaperHolding.portfolio_id == second.id).count()

    assert first.id == second.id
    assert holdings_before == holdings_after == 1


# ---------------------------------------------------------------------------
# C1 + D1 — proposal & full cycle (acceptance)
# ---------------------------------------------------------------------------


def test_build_trade_proposal_produces_mc_and_envelope():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    _seed_prices(db, "HELD", _wiggly_series(90.0))
    _seed_prices(db, "CAND", _wiggly_series(40.0))

    proposal = build_trade_proposal(
        db, portfolio.id, [{"symbol": "CAND", "source": "screen_index"}]
    )
    assert "CAND" in proposal.mc_summaries
    mc = proposal.mc_summaries["CAND"]
    assert mc.p5 <= mc.p50 <= mc.p95
    assert 0.0 <= mc.prob_positive <= 1.0
    assert proposal.suggested_weights  # optimizer or documented fallback
    assert "cvar_95_daily" in proposal.risk_envelope


def test_run_advisor_cycle_end_to_end():
    """Acceptance (D2): ≥1 trade, ≥1 prediction row, snapshot advanced."""
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly_series(90.0))
    _seed_prices(db, "CAND", _wiggly_series(40.0))

    snapshots_before = db.query(PaperSnapshot).count()

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Quant screen + MC upside.", "confidence": 0.65},
        ]),
    )

    assert result["status"] == "completed"
    assert len(result["trades"]) >= 1
    assert len(result["predictions"]) >= 1

    preds = db.query(DiscoveryPrediction).all()
    assert len(preds) >= 1
    pred = preds[0]
    assert pred.direction == "buy"
    assert pred.conviction == 0.65
    assert pred.horizon_days == 21 or pred.horizon_days > 0
    assert pred.price_at_prediction is not None
    assert pred.features_json.get("mc_prob_positive") is not None
    assert pred.thesis

    assert db.query(PaperSnapshot).count() > snapshots_before
    assert (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .count()
        == 1
    )


def test_run_advisor_cycle_idempotent_per_day():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly_series(90.0))
    _seed_prices(db, "CAND", _wiggly_series(40.0))

    stub = _stub_llm([
        {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
         "thesis": "t", "confidence": 0.6},
    ])
    first = run_advisor_cycle(db, user.id, llm_call=stub)
    second = run_advisor_cycle(db, user.id, llm_call=stub)

    assert first["status"] == "completed"
    assert second["status"] == "already_ran_today"
    assert second["trades"] == []


def test_run_advisor_cycle_no_candidates():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)

    result = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))
    assert result["status"] == "no_candidates"
    assert result["trades"] == []
