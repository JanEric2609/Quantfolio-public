"""Tests for the advisor cycle's audit trail and trade-sizing corrections.

Regression cover for the production failure where the loop ran daily for weeks
and left no trace: early-exit paths wrote no decision row, skipped trades left
no reason, and the sell path could liquidate a whole position.
"""
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
    LlmPortfolioDecision,
    PaperHolding,
    PaperSnapshot,
    PriceCache,
    User,
)
from app.decision.advisor.cycle import (
    ADVISOR_MANDATE,
    _execute_planned,
    _plan_decision,
    run_advisor_cycle,
)
from app.decision.paper_portfolio import seed_paper_portfolio_from_real
from app.decision.advisor.decision import McSummary, TradeProposal, build_trade_proposal
from app.decision.advisor.llm_decision import TradeDecision


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
    days: list[date] = []
    day = date.today()
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
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


def _wiggly(base: float, n: int = 300, drift: float = 0.0004) -> list[float]:
    return [
        base * (1 + i * drift)
        + base * (0.005 if i % 7 == 0 else (-0.004 if i % 5 == 0 else 0.0))
        for i in range(n)
    ]


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
        id=uuid4().hex, user_id=user.id, status="completed", created_at=datetime.now(UTC)
    )
    db.add(run)
    db.commit()
    for sym in symbols:
        db.add(
            DiscoverCandidate(
                id=uuid4().hex, run_id=run.id, symbol=sym,
                source="screen_index", status="shortlisted",
            )
        )
    db.commit()
    return run


def _stub_llm(decisions: list[dict]):
    def _call(_db, _messages):
        return json.dumps({"decisions": decisions})

    return _call


# ---------------------------------------------------------------------------
# Audit trail — every terminating path leaves a record
# ---------------------------------------------------------------------------


def test_no_candidates_writes_a_heartbeat_record_and_snapshot():
    """An idle day must be visible; it used to return without writing anything."""
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)

    result = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))

    assert result["status"] == "no_candidates"
    row = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .one()
    )
    assert row.status == "idle_no_candidates"
    assert "shortlisted candidates" in (row.error or "")
    payload = json.loads(row.decision_json)
    assert payload["decisions"] == []
    assert payload["errors"]
    # NAV must keep advancing even on idle days — graduation counts snapshots.
    assert db.query(PaperSnapshot).count() == 1
    assert result["snapshot"] is not None


def test_idle_day_counts_as_already_ran():
    """An idle record must short-circuit the same day, or evolution re-runs it."""
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)

    first = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))
    second = run_advisor_cycle(db, user.id, llm_call=_stub_llm([]))

    assert first["status"] == "no_candidates"
    assert second["status"] == "already_ran_today"
    assert (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .count()
        == 1
    )


def test_hold_only_cycle_records_idle_no_trades():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(90.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "hold", "target_weight": 0.0,
             "thesis": "", "confidence": 0.4},
        ]),
    )

    assert result["status"] == "no_trades"
    row = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .one()
    )
    assert row.status == "idle_no_trades"


def test_rejected_trade_reason_is_persisted():
    """An insufficient-cash rejection must be visible, not a silent skip."""
    db = _memory_db()
    user = _user(db)
    # Almost no cash, so a 40% target on the book is unaffordable.
    _seed_real_book(db, user, cash=25.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(90.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.4,
             "thesis": "Big conviction.", "confidence": 0.8},
        ]),
    )

    assert result["trades"] == []
    assert result["skipped"], "a rejected trade must be reported"
    # The executor now pre-empts the book's bare "Insufficient cash" rejection
    # with a reason that names the binding constraint: what was left after
    # funding sells could not clear the minimum ticket.
    reason = result["skipped"][0]["reason"]
    assert "unfundable" in reason and "minimum ticket" in reason

    row = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .one()
    )
    payload = json.loads(row.decision_json)
    assert payload["skipped"][0]["ticker"] == "CAND"
    assert "unfundable" in payload["skipped"][0]["reason"]


def test_stale_discover_run_is_flagged_on_the_record():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    run = _seed_discover_run(db, user, ["CAND"])
    run.created_at = datetime.now(UTC) - timedelta(days=30)
    db.commit()
    _seed_prices(db, "HELD", _wiggly(90.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "t", "confidence": 0.6},
        ]),
    )

    row = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .one()
    )
    payload = json.loads(row.decision_json)
    assert payload["discover_run_stale"] is True
    assert any("stale" in e for e in payload["errors"])


# ---------------------------------------------------------------------------
# Trade sizing
# ---------------------------------------------------------------------------


def _proposal(book: dict[str, float], spot: float = 100.0) -> TradeProposal:
    return TradeProposal(
        portfolio_id="p1",
        suggested_weights={"AAA": 0.5},
        mc_summaries={"AAA": McSummary("AAA", "equity", -0.05, 0.01, 0.08, 0.6, spot)},
        risk_envelope={},
        current_book=book,
        optimizer_status="completed",
    )


def _execute_decision(db, portfolio, decision, proposal, total_value, min_ticket=0.0, fee=0.0):
    """Size and send one decision.

    Planning and execution are separate in the cycle so the turnover budget can
    be applied across the whole decision set; these sizing tests only care
    about the combined result.
    """
    planned, reason = _plan_decision(decision, proposal, total_value, min_ticket)
    if planned is None:
        return None, reason
    return _execute_planned(db, portfolio, planned, fee)


def test_sell_at_or_above_current_weight_is_a_hold_not_a_liquidation():
    """A 'sell' whose target is at/above the position must not dump the lot."""
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    decision = TradeDecision(
        ticker="AAA", action="sell", target_weight=0.5, thesis="trim", confidence=0.5
    )
    # Position worth 1000; target weight 0.5 of a 10_000 book = 5000 ≥ 1000.
    trade, reason = _execute_decision(
        db, portfolio, decision, _proposal({"AAA": 1000.0}), total_value=10_000.0
    )

    assert trade is None
    assert reason is not None and "treated as hold" in reason


def test_sell_trims_to_target_rather_than_liquidating():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio = seed_paper_portfolio_from_real(db, user.id)
    db.add(
        PaperHolding(
            id=uuid4().hex, portfolio_id=portfolio.id, ticker="AAA", name="AAA",
            quantity=Decimal("100"), avg_buy_price=Decimal("100"), asset_type="stock",
        )
    )
    db.commit()

    decision = TradeDecision(
        ticker="AAA", action="sell", target_weight=0.08, thesis="trim", confidence=0.5
    )
    # Position 10_000; target 0.08 * 100_000 = 8_000 → sell 2_000 worth, not all.
    # Kept comfortably under the 1/3-of-position sell cap (see
    # DEFAULT_MAX_SELL_PCT_OF_POSITION) so this test still isolates "trim
    # stops at target" from the separate per-position cap behaviour, which
    # has its own coverage in test_advisor_trade_flow.py.
    trade, reason = _execute_decision(
        db, portfolio, decision, _proposal({"AAA": 10_000.0}), total_value=100_000.0
    )

    assert reason is None
    assert trade is not None
    assert trade["side"] == "sell"
    assert abs(trade["value"] - 2_000.0) < 1.0
    remaining = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == portfolio.id, PaperHolding.ticker == "AAA")
        .one()
    )
    assert float(remaining.quantity) > 0


def test_buy_at_target_reports_why_it_skipped():
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    decision = TradeDecision(
        ticker="AAA", action="buy", target_weight=0.05, thesis="add", confidence=0.6
    )
    trade, reason = _execute_decision(
        db, portfolio, decision, _proposal({"AAA": 9_000.0}), total_value=100_000.0
    )

    assert trade is None
    assert reason is not None and "already met" in reason


def test_current_book_is_marked_to_market_not_cost_basis():
    """Mixing cost basis with a market-value total misprices every delta."""
    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user)
    portfolio = seed_paper_portfolio_from_real(db, user.id)
    # HELD was seeded at avg_buy_price 90; mark it at ~200 in the price cache.
    _seed_prices(db, "HELD", _wiggly(200.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))
    _seed_discover_run(db, user, ["CAND"])

    proposal = build_trade_proposal(
        db, portfolio.id, [{"symbol": "CAND", "source": "screen_index"}]
    )

    held_cost_basis = 100 * 90.0
    assert proposal.current_book["HELD"] > held_cost_basis * 1.5


# ---------------------------------------------------------------------------
# Hold decisions as prediction rows (learning signal decoupled from execution)
# ---------------------------------------------------------------------------


def test_hold_decisions_are_logged_as_neutral_predictions():
    """A cautious model must still generate feedback, or it can never improve."""
    from app.foundation.models.entities import DiscoveryPrediction

    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(90.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "hold", "target_weight": 0.0,
             "thesis": "Waiting for a better entry.", "confidence": 0.45},
        ]),
    )

    assert result["trades"] == []
    assert len(result["predictions"]) == 1

    pred = db.query(DiscoveryPrediction).one()
    assert pred.direction == "neutral"
    assert pred.price_at_prediction is not None and pred.price_at_prediction > 0
    assert pred.expected_return is not None  # MC p50 is the magnitude forecast
    assert pred.features_json["signal_breakdown"]["traded"] is False


def test_hold_is_not_double_logged_when_the_same_name_traded():
    from app.foundation.models.entities import DiscoveryPrediction

    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(90.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Buy.", "confidence": 0.7},
            {"ticker": "CAND", "action": "hold", "target_weight": 0.0,
             "thesis": "", "confidence": 0.3},
        ]),
    )

    rows = db.query(DiscoveryPrediction).all()
    assert len(rows) == 1
    assert rows[0].direction == "buy"


def test_neutral_prediction_resolves_unsigned():
    """A hold's realised return is the raw move — never sign-flipped."""
    from app.foundation.models.entities import DiscoveryPrediction
    from app.decision.discover.resolution import resolve_due_predictions

    db = _memory_db()
    user = _user(db)
    _seed_prices(db, "AAA", [100.0] * 40 + [110.0])
    last_close = date.today()
    while last_close.weekday() >= 5:
        last_close -= timedelta(days=1)

    db.add(
        DiscoveryPrediction(
            id=uuid4().hex, user_id=user.id, run_id="r", symbol="AAA",
            conviction=0.6, horizon_days=21,
            predicted_at=datetime.now(UTC) - timedelta(days=40),
            # The last seeded close (a weekday): "yesterday" is a Saturday on
            # Sundays, with no close on or after it yet, and the row waits.
            resolve_at=datetime.combine(last_close, datetime.min.time(), UTC),
            direction="neutral", outcome_status="pending",
            price_at_prediction=100.0,
        )
    )
    db.commit()

    resolved = resolve_due_predictions(db)

    assert len(resolved) == 1
    row = db.query(DiscoveryPrediction).one()
    assert row.outcome_status == "resolved"
    assert row.realised_return is not None and row.realised_return > 0
