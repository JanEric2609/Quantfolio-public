"""Tests for the advisor cycle's trade-flow policy.

Cover for the production failure where a sleeve mirroring a fully invested
real book executed one trade in three weeks. The sleeve had almost no cash
against its book, every buy was rejected by the cash gate, and a rejected
buy was logged nowhere — so the model's highest-conviction calls left no
evidence and the calibration axis could never become computable.

Four behaviours are asserted here:

- sells settle before buys, so a fully-invested sleeve can rotate;
- commissions are charged and reduce cash;
- the turnover cap and minimum-ticket floor bound each cycle;
- every decision is logged as intent, deduped against still-open rows.
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
    PaperPortfolio,
    PaperTrade,
    PriceCache,
    User,
)
from app.decision.advisor.costs import (
    DEFAULT_FEE_SCHEDULE,
    affordable_notional,
    describe_fee_schedule,
    resolve_fee_schedule,
    trade_fee,
)
from app.decision.advisor.cycle import (
    _MAX_FAILED_ATTEMPTS_PER_DAY,
    ADVISOR_MANDATE,
    _already_ran_today,
    _ordered_decisions,
    _plan_decision,
    _raise_cash,
    run_advisor_cycle,
)
from app.decision.advisor.decision import McSummary, TradeProposal
from app.decision.advisor.llm_decision import TradeDecision
from app.decision.paper_portfolio import (
    _current_cash_balance,
    execute_trade,
    seed_paper_portfolio_from_real,
)


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


def _seed_fully_invested_book(db, user: User, cash: float = 20.0) -> None:
    """The real-world shape: a large depot, almost no free cash."""
    cash_account = DkbAccount(
        id=uuid4().hex, user_id=user.id, type="checking",
        iban="DE" + uuid4().hex[:20], balance=Decimal(str(cash)), currency="EUR",
    )
    depot = DkbAccount(
        id=uuid4().hex, user_id=user.id, type="depot",
        iban="DE" + uuid4().hex[:20], balance=Decimal("40000"), currency="EUR",
    )
    db.add_all([cash_account, depot])
    db.commit()
    db.add(
        DkbPosition(
            id=uuid4().hex, account_id=depot.id, isin="DE000TEST0001",
            ticker="HELD", name="Held Stock", quantity=Decimal("400"),
            avg_buy_price=Decimal("90"), current_price=Decimal("100"),
            current_value=Decimal("40000"),
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


def _capturing_llm(decisions: list[dict], sink: dict):
    def _call(_db, messages):
        sink["messages"] = messages
        return json.dumps({"decisions": decisions})

    return _call


def _proposal(book: dict[str, float], spot: float = 100.0) -> TradeProposal:
    return TradeProposal(
        portfolio_id="p1",
        suggested_weights={"AAA": 0.5},
        mc_summaries={"AAA": McSummary("AAA", "equity", -0.05, 0.01, 0.08, 0.6, spot)},
        risk_envelope={},
        current_book=book,
        optimizer_status="completed",
    )


# ---------------------------------------------------------------------------
# Commission schedule
# ---------------------------------------------------------------------------


def test_fee_tiers_match_the_dkb_schedule():
    # DKB Broker domestic: €10 up to €5k, €15 to €20k, €30 above.
    assert trade_fee(1_000.0) == 10.0
    assert trade_fee(5_000.0) == 10.0
    assert trade_fee(5_000.01) == 15.0
    assert trade_fee(20_000.0) == 15.0
    assert trade_fee(20_000.01) == 30.0


def test_zero_notional_is_never_charged():
    assert trade_fee(0.0) == 0.0
    assert trade_fee(-100.0) == 0.0


def test_venue_fee_is_added_when_configured():
    schedule = resolve_fee_schedule({"fee_schedule": {"venue_fee": 2.5}})
    assert trade_fee(1_000.0, schedule) == 12.5


def test_strategy_can_override_the_whole_schedule():
    schedule = resolve_fee_schedule(
        {"fee_schedule": {"tiers": [[1_000.0, 1.0]], "above": 2.0}}
    )
    assert trade_fee(500.0, schedule) == 1.0
    assert trade_fee(5_000.0, schedule) == 2.0


def test_malformed_override_falls_back_to_defaults():
    """A bad config must not silently make trading free."""
    schedule = resolve_fee_schedule({"fee_schedule": {"tiers": "nonsense"}})
    assert schedule["tiers"] == list(DEFAULT_FEE_SCHEDULE["tiers"])
    assert trade_fee(1_000.0, schedule) == 10.0


def test_fee_schedule_description_is_human_readable():
    text = describe_fee_schedule()
    assert "€10.00" in text and "€30.00" in text


# ---------------------------------------------------------------------------
# Fees reach the book
# ---------------------------------------------------------------------------


def test_buy_fee_leaves_the_cash_balance():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=10_000.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    before = _current_cash_balance(portfolio, db)
    execute_trade(db, portfolio.id, "AAA", "buy", 10.0, 100.0, fee=10.0)
    after = _current_cash_balance(portfolio, db)

    # 10 * 100 notional + 10 commission.
    assert float(before - after) == 1_010.0


def test_sell_fee_is_netted_from_proceeds():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=10_000.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    before = _current_cash_balance(portfolio, db)
    execute_trade(db, portfolio.id, "HELD", "sell", 10.0, 100.0, fee=10.0)
    after = _current_cash_balance(portfolio, db)

    assert float(after - before) == 990.0


def test_buy_must_cover_notional_plus_fee():
    """A buy that fits the notional but not the commission is rejected."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=1_000.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    try:
        execute_trade(db, portfolio.id, "AAA", "buy", 10.0, 100.0, fee=10.0)
    except ValueError as exc:
        assert "Insufficient cash" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("expected the fee to push the buy past available cash")


def test_sell_whose_fee_exceeds_proceeds_is_rejected():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=1_000.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    try:
        execute_trade(db, portfolio.id, "HELD", "sell", 0.01, 100.0, fee=10.0)
    except ValueError as exc:
        assert "exceeds sale proceeds" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a sell that only destroys cash to be rejected")


def test_manual_trades_stay_free_by_default():
    """execute_trade's other callers must not start paying commission."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=10_000.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    trade = execute_trade(db, portfolio.id, "AAA", "buy", 10.0, 100.0)

    assert trade["fee"] == 0.0


# ---------------------------------------------------------------------------
# Ordering, cap, and minimum ticket
# ---------------------------------------------------------------------------


def test_sells_are_ordered_before_buys():
    decisions = [
        TradeDecision("BUY1", "buy", 0.1, "t", 0.9),
        TradeDecision("SELL1", "sell", 0.0, "t", 0.4),
        TradeDecision("HOLD1", "hold", 0.0, "", 0.5),
        TradeDecision("SELL2", "sell", 0.0, "t", 0.8),
    ]

    ordered = [d.ticker for d in _ordered_decisions(decisions)]

    # Sells first (by descending confidence), then buys. Holds never trade.
    assert ordered == ["SELL2", "SELL1", "BUY1"]
    assert "HOLD1" not in ordered


def test_buys_are_ranked_by_confidence():
    decisions = [
        TradeDecision("LOW", "buy", 0.1, "t", 0.2),
        TradeDecision("HIGH", "buy", 0.1, "t", 0.95),
        TradeDecision("MID", "buy", 0.1, "t", 0.6),
    ]

    assert [d.ticker for d in _ordered_decisions(decisions)] == ["HIGH", "MID", "LOW"]


def test_orders_below_the_minimum_ticket_are_skipped_with_a_reason():
    decision = TradeDecision("AAA", "buy", 0.001, "add", 0.7)

    planned, reason = _plan_decision(
        decision, _proposal({}), total_value=100_000.0, min_ticket=1_000.0
    )

    assert planned is None
    assert reason is not None and "below minimum ticket" in reason


def test_orders_at_or_above_the_minimum_ticket_are_planned():
    decision = TradeDecision("AAA", "buy", 0.05, "add", 0.7)

    planned, reason = _plan_decision(
        decision, _proposal({}), total_value=100_000.0, min_ticket=1_000.0
    )

    assert reason is None
    assert planned is not None
    assert planned.side == "buy"
    assert planned.notional == 5_000.0


def test_a_fully_invested_sleeve_funds_its_buy_by_selling_first():
    """The core regression: cash €20 against a €40k book must still rotate."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            # The buy is listed first, exactly as an LLM would emit it. It only
            # clears because the executor settles the sell before it.
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Rotate in.", "confidence": 0.8},
            {"ticker": "HELD", "action": "sell", "target_weight": 0.85,
             "thesis": "Free capital.", "confidence": 0.6},
        ]),
    )

    sides = [t["side"] for t in result["trades"]]
    assert sides == ["sell", "buy"], f"expected sell-then-buy, got {sides}"
    assert not any("Insufficient cash" in s["reason"] for s in result["skipped"])


def test_turnover_cap_drops_the_least_confident_trades():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20_000.0)
    _seed_discover_run(db, user, ["AAA", "BBB"])
    for sym, base in (("HELD", 100.0), ("AAA", 40.0), ("BBB", 60.0)):
        _seed_prices(db, sym, _wiggly(base, drift=0.0))

    result = run_advisor_cycle(
        db, user.id,
        # 2% of NAV, so only the first buy fits.
        config={"max_turnover_pct": 0.02, "min_ticket_pct": 0.001},
        llm_call=_stub_llm([
            {"ticker": "AAA", "action": "buy", "target_weight": 0.02,
             "thesis": "Strong.", "confidence": 0.9},
            {"ticker": "BBB", "action": "buy", "target_weight": 0.02,
             "thesis": "Weaker.", "confidence": 0.3},
        ]),
    )

    traded = {t["ticker"] for t in result["trades"]}
    assert "AAA" in traded, "the highest-confidence buy must survive the cap"
    assert result["turnover_used"] <= result["turnover_budget"] + 1e-6
    capped = [s for s in result["skipped"] if "turnover cap" in s["reason"]]
    assert capped and capped[0]["ticker"] == "BBB"


def test_sells_cannot_exhaust_the_budget_a_pending_buy_needs():
    """A rotation spends the cap twice. If the sells are allowed to use it all,
    the sleeve ends up half-rotated and sitting in cash."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            # A sell far larger than the whole cap, plus a buy depending on it.
            {"ticker": "HELD", "action": "sell", "target_weight": 0.1,
             "thesis": "Cut hard.", "confidence": 0.9},
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Rotate in.", "confidence": 0.8},
        ]),
    )

    sides = [t["side"] for t in result["trades"]]
    assert sides == ["sell", "buy"], f"expected the buy to still fit, got {sides}"
    sell_notional = next(t["value"] for t in result["trades"] if t["side"] == "sell")
    assert sell_notional <= result["turnover_budget"] / 2 + 1e-6


def test_cycle_reports_fees_and_turnover():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Add.", "confidence": 0.8},
        ]),
    )

    assert result["trades"], "expected the buy to execute"
    assert result["fees_paid"] > 0
    row = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .one()
    )
    flow = json.loads(row.decision_json)["trade_flow"]
    assert flow["fees_paid"] == result["fees_paid"]
    assert flow["turnover_budget"] > 0
    # The fee is persisted on the trade itself, not folded into value.
    trade = db.query(PaperTrade).filter(PaperTrade.ticker == "CAND").one()
    assert float(trade.fee) > 0


# ---------------------------------------------------------------------------
# Deterministic funding sells
# ---------------------------------------------------------------------------


def test_buy_is_funded_by_trimming_an_overweight_holding():
    """Production reality: Qwen3-8B proposed three buys against almost no cash
    and offered no sell at all, not even a decision on its largest holding."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Buy, with no way to pay for it.", "confidence": 0.8},
        ]),
    )

    assert result["funding_sells"], "expected the sleeve to raise cash itself"
    assert result["funding_sells"][0]["ticker"] == "HELD"
    sides = [t["side"] for t in result["trades"]]
    assert "sell" in sides and "buy" in sides
    assert not any("Insufficient cash" in s["reason"] for s in result["skipped"])


def test_funding_never_sells_the_name_being_bought():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "HELD", "action": "buy", "target_weight": 0.99,
             "thesis": "Add to the existing position.", "confidence": 0.9},
        ]),
    )

    assert all(t["ticker"] != "HELD" for t in result["funding_sells"])


def test_funding_sells_are_not_logged_as_model_intent():
    """A trim the optimiser asked for is not a call the model made, and must
    not be scored as one."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Buy.", "confidence": 0.8},
        ]),
    )

    assert result["funding_sells"]
    symbols = {r.symbol for r in db.query(DiscoveryPrediction).all()}
    assert "HELD" not in symbols, "an optimiser trim must not enter the intent ledger"


def test_funding_sells_are_marked_and_audited():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Buy.", "confidence": 0.8},
        ]),
    )

    assert all(t.get("funding_sell") for t in result["funding_sells"])
    row = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .one()
    )
    audited = json.loads(row.decision_json)["funding_sells"]
    assert audited and audited[0]["ticker"] == "HELD"


def test_funding_respects_the_turnover_cap():
    """The trim plus the buy it funds must both fit inside the cycle's cap."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Buy.", "confidence": 0.8},
        ]),
    )

    assert result["turnover_used"] <= result["turnover_budget"] + 1e-6


def test_no_funding_when_the_buy_is_already_affordable():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Buy.", "confidence": 0.8},
        ]),
    )

    assert result["trades"]
    assert result["funding_sells"] == [], "cash was sufficient; nothing should be trimmed"


def test_positions_at_or_below_target_are_never_trimmed():
    """Only genuine overweights fund a buy — the trim stops at the target."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)
    proposal = TradeProposal(
        portfolio_id=portfolio.id,
        # HELD's target equals its full current value, so it is not overweight.
        suggested_weights={"HELD": 1.0},
        mc_summaries={"HELD": McSummary("HELD", "equity", -0.05, 0.01, 0.08, 0.6, 100.0)},
        risk_envelope={},
        current_book={"HELD": 40_000.0},
        optimizer_status="completed",
    )

    proceeds, notional, records = _raise_cash(
        db, portfolio, proposal, 40_000.0, 5_000.0,
        exclude=set(), min_ticket=400.0, budget=6_000.0,
        fee_schedule=DEFAULT_FEE_SCHEDULE,
    )

    assert records == []
    assert proceeds == 0.0 and notional == 0.0


def test_trim_stops_at_the_target_weight():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)
    proposal = TradeProposal(
        portfolio_id=portfolio.id,
        # Target 90% of a 40k book = 36k, so only 4k is trimmable.
        suggested_weights={"HELD": 0.9},
        mc_summaries={"HELD": McSummary("HELD", "equity", -0.05, 0.01, 0.08, 0.6, 100.0)},
        risk_envelope={},
        current_book={"HELD": 40_000.0},
        optimizer_status="completed",
    )

    _, notional, records = _raise_cash(
        db, portfolio, proposal, 40_000.0, 10_000.0,
        exclude=set(), min_ticket=400.0, budget=20_000.0,
        fee_schedule=DEFAULT_FEE_SCHEDULE,
    )

    assert records, "a 4k overweight should still be trimmed"
    assert notional <= 4_000.0 + 1e-6, "the trim must not cut below the target weight"


def test_trim_takes_the_most_overweight_position_first():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)
    db.add(
        PaperHolding(
            id=uuid4().hex, portfolio_id=portfolio.id, ticker="OTHER", name="Other",
            quantity=Decimal("100"), avg_buy_price=Decimal("50"), asset_type="stock",
        )
    )
    db.commit()
    proposal = TradeProposal(
        portfolio_id=portfolio.id,
        suggested_weights={"HELD": 0.5, "OTHER": 0.0},
        mc_summaries={
            "HELD": McSummary("HELD", "equity", -0.05, 0.01, 0.08, 0.6, 100.0),
            "OTHER": McSummary("OTHER", "equity", -0.05, 0.01, 0.08, 0.6, 50.0),
        },
        risk_envelope={},
        # HELD is 20k over target; OTHER is only 5k over.
        current_book={"HELD": 40_000.0, "OTHER": 5_000.0},
        optimizer_status="completed",
    )

    _, _, records = _raise_cash(
        db, portfolio, proposal, 40_000.0, 3_000.0,
        exclude=set(), min_ticket=400.0, budget=20_000.0,
        fee_schedule=DEFAULT_FEE_SCHEDULE,
    )

    assert records and records[0]["ticker"] == "HELD"


# ---------------------------------------------------------------------------
# Per-position sell cap (2026-09-01 underperformance fix)
# ---------------------------------------------------------------------------


def test_plan_decision_caps_a_sell_at_a_third_of_the_position():
    """A model-directed full liquidation must still take multiple cycles."""
    decision = TradeDecision(
        ticker="AAA", action="sell", target_weight=0.0,
        thesis="Exit entirely.", confidence=0.9,
    )
    proposal = _proposal({"AAA": 30_000.0})

    planned, skip_reason = _plan_decision(decision, proposal, 60_000.0, 100.0)

    assert skip_reason is None
    assert planned is not None
    assert planned.side == "sell"
    assert planned.notional == 10_000.0  # 1/3 of 30,000


def test_plan_decision_sell_cap_is_configurable():
    decision = TradeDecision(
        ticker="AAA", action="sell", target_weight=0.0,
        thesis="Exit entirely.", confidence=0.9,
    )
    proposal = _proposal({"AAA": 30_000.0})

    planned, _ = _plan_decision(decision, proposal, 60_000.0, 100.0, 0.5)

    assert planned is not None
    assert planned.notional == 15_000.0  # 1/2 of 30,000


def test_plan_decision_does_not_cap_a_sell_within_the_limit():
    decision = TradeDecision(
        ticker="AAA", action="sell", target_weight=0.9,
        thesis="Trim a little.", confidence=0.9,
    )
    proposal = _proposal({"AAA": 30_000.0})

    planned, _ = _plan_decision(decision, proposal, 30_000.0, 100.0)

    assert planned is not None
    assert planned.notional == 3_000.0  # 30,000 - 0.9*30,000, well under the cap


def test_raise_cash_respects_the_per_position_sell_cap():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)
    proposal = TradeProposal(
        portfolio_id=portfolio.id,
        # Fully overweight — the optimiser would trim HELD to zero, but the
        # per-position cap must still bound one cycle's trim to a third.
        suggested_weights={"HELD": 0.0},
        mc_summaries={"HELD": McSummary("HELD", "equity", -0.05, 0.01, 0.08, 0.6, 100.0)},
        risk_envelope={},
        current_book={"HELD": 40_000.0},
        optimizer_status="completed",
    )

    _, notional, records = _raise_cash(
        db, portfolio, proposal, 40_000.0, 100_000.0,
        exclude=set(), min_ticket=400.0, budget=100_000.0,
        fee_schedule=DEFAULT_FEE_SCHEDULE,
    )

    assert records
    cap = 40_000.0 / 3.0
    assert notional <= cap + 1e-6, "the funding trim must not exceed the per-position cap"


def test_advisor_cycle_caps_a_full_liquidation_across_the_book():
    """Integration-level regression: the model asking to fully exit a €40k
    holding in one cycle must only execute up to the per-position cap."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        config={"max_turnover_pct": 0.9, "min_ticket_pct": 0.001},
        llm_call=_stub_llm([
            {"ticker": "HELD", "action": "sell", "target_weight": 0.0,
             "thesis": "Exit entirely, right now.", "confidence": 0.95},
        ]),
    )

    sell = next(t for t in result["trades"] if t["ticker"] == "HELD")
    cap = 40_000.0 / 3.0
    assert sell["value"] <= cap + 1e-6, (
        f"a full liquidation must be capped to ~1/3 of the position per cycle, "
        f"got €{sell['value']:,.0f}"
    )


# ---------------------------------------------------------------------------
# Intent logging
# ---------------------------------------------------------------------------


def test_a_cash_rejected_buy_is_still_logged_as_intent():
    """The defect that made the calibration axis uncomputable.

    A buy that cannot be funded was neither executed nor a hold, so it produced
    no ledger row at all — the model's strongest calls left no evidence.
    """
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.5,
             "thesis": "High conviction, unaffordable.", "confidence": 0.9},
        ]),
    )

    assert not result["trades"], "the buy should not have been funded"
    row = db.query(DiscoveryPrediction).filter(DiscoveryPrediction.symbol == "CAND").one()
    assert row.direction == "buy"
    assert row.features_json["signal_breakdown"]["traded"] is False


def test_executed_trades_are_marked_as_filled():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.05,
             "thesis": "Add.", "confidence": 0.8},
        ]),
    )

    row = db.query(DiscoveryPrediction).filter(DiscoveryPrediction.symbol == "CAND").one()
    assert row.features_json["signal_breakdown"]["traded"] is True


def test_repeating_an_unchanged_view_does_not_write_a_second_row():
    """Overlapping repeats would clear the 20-resolved threshold without
    adding independent evidence."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    decisions = [
        {"ticker": "CAND", "action": "buy", "target_weight": 0.5,
         "thesis": "Same view.", "confidence": 0.9},
    ]
    run_advisor_cycle(db, user.id, llm_call=_stub_llm(decisions))
    # Clear the once-a-day guard so the second cycle actually runs.
    db.query(LlmPortfolioDecision).delete()
    db.commit()
    result = run_advisor_cycle(db, user.id, llm_call=_stub_llm(decisions))

    rows = db.query(DiscoveryPrediction).filter(DiscoveryPrediction.symbol == "CAND").all()
    assert len(rows) == 1
    assert result["predictions_deduped"] == [{"ticker": "CAND", "direction": "buy"}]


def test_a_changed_direction_earns_a_new_row():
    """Dedupe must suppress repetition, not a genuine change of mind."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.5,
             "thesis": "In.", "confidence": 0.9},
        ]),
    )
    db.query(LlmPortfolioDecision).delete()
    db.commit()
    run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "hold", "target_weight": 0.0,
             "thesis": "", "confidence": 0.4},
        ]),
    )

    directions = {
        r.direction
        for r in db.query(DiscoveryPrediction).filter(DiscoveryPrediction.symbol == "CAND").all()
    }
    assert directions == {"buy", "neutral"}


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------


def test_the_prompt_states_cash_costs_and_constraints():
    """The model proposed unaffordable orders because it was never told the
    balance, and churned because trading appeared free."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))
    sink: dict = {}

    run_advisor_cycle(db, user.id, llm_call=_capturing_llm([], sink))

    system = sink["messages"][0]["content"]
    payload = json.loads(sink["messages"][1]["content"])
    assert "cash_eur" in payload
    assert payload["portfolio_value_eur"] > 0
    assert "commission_schedule" in payload["trading_costs"]
    assert payload["constraints"]["min_order_eur"] > 0
    assert payload["constraints"]["max_total_traded_eur_this_cycle"] > 0
    assert any(c.get("estimated_fee_eur") is not None for c in payload["candidates"])
    # And the funding rule must be stated, not merely implied by the numbers.
    assert "sells always settle first" in system


def test_holdings_are_offered_to_the_model_as_sellable():
    """Rotation is only possible if the book is inside the allowed set."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))
    sink: dict = {}

    run_advisor_cycle(db, user.id, llm_call=_capturing_llm([], sink))

    payload = json.loads(sink["messages"][1]["content"])
    assert "HELD" in payload["allowed_tickers"]
    assert payload["current_book_eur"]["HELD"] > 0


def test_seeded_sleeve_keeps_only_real_cash():
    """No notional funding: the mirror's cash is the real non-depot balance."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)

    portfolio = seed_paper_portfolio_from_real(db, user.id)

    assert float(portfolio.initial_cash) == 20.0
    held = (
        db.query(PaperHolding)
        .filter(PaperHolding.portfolio_id == portfolio.id, PaperHolding.ticker == "HELD")
        .one()
    )
    assert float(held.quantity) == 400.0


# ---------------------------------------------------------------------------
# A funding raise that lands short must downsize the buy, not strand the cash
# ---------------------------------------------------------------------------


def test_affordable_notional_always_leaves_room_for_the_commission():
    for cash in (10.0, 25.0, 500.0, 2_011.0, 5_010.0, 25_000.0, 100_000.0):
        notional = affordable_notional(cash)
        assert notional + trade_fee(notional) <= cash, cash


def test_affordable_notional_picks_the_best_tier():
    # €5,010 buys exactly the top of the €10 tier; paying the €15 tier's fee
    # would leave less, so the cheaper tier must win.
    assert affordable_notional(5_010.0) == 5_000.0
    # Below the smallest fee nothing is affordable at all.
    assert affordable_notional(10.0) == 0.0
    assert affordable_notional(0.0) == 0.0
    assert affordable_notional(-5.0) == 0.0


def test_affordable_notional_respects_a_custom_schedule():
    schedule = resolve_fee_schedule({"fee_schedule": {"venue_fee": 2.5}})
    notional = affordable_notional(1_000.0, schedule)
    assert notional + trade_fee(notional, schedule) <= 1_000.0
    assert notional == 987.5


def test_a_short_funding_raise_downsizes_the_buy_rather_than_stranding_cash():
    """The regression this fixes: sell, then fail to buy, and sit on the cash.

    ``_raise_cash`` is capped at the turnover budget the buy has not already
    reserved, so whenever the buy exceeds half the remaining budget the raise
    lands short by construction. Rejecting the buy at that point leaves the
    sleeve holding cash it sold a position to raise — strictly worse than never
    having traded.
    """
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            # 10% of NAV against a 15% cap — the raise can only ever cover the
            # 5% the buy did not reserve.
            {"ticker": "CAND", "action": "buy", "target_weight": 0.10,
             "thesis": "High conviction.", "confidence": 0.9},
        ]),
    )

    assert result["funding_sells"], "the trim must still happen"
    buys = [t for t in result["trades"] if t["side"] == "buy"]
    assert buys, "the buy the trim financed must reach the book"
    assert buys[0]["ticker"] == "CAND"
    assert result["status"] == "completed"

    # Downsized, not filled at the full ask — and the raised cash is spent.
    raised = sum(float(t["value"]) - float(t["fee"]) for t in result["funding_sells"])
    assert buys[0]["value"] <= raised + 20.0
    portfolio = db.get(PaperPortfolio, result["portfolio_id"])
    assert float(_current_cash_balance(portfolio, db)) < 20.0


def test_an_unfundable_buy_is_skipped_with_the_binding_reason():
    """When even the trim cannot clear the minimum ticket, say so."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        # The whole book into one name: the trim is capped far below the ask.
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.99,
             "thesis": "All in.", "confidence": 0.9},
        ]),
    )

    reasons = " ".join(s["reason"] for s in result["skipped"])
    assert not result["skipped"] or "unfundable" in reasons or "turnover cap" in reasons


# ---------------------------------------------------------------------------
# Cycle status semantics: a partly-invalid response is not a failed cycle
# ---------------------------------------------------------------------------


def _cycle_row(db) -> LlmPortfolioDecision:
    return (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == ADVISOR_MANDATE)
        .order_by(LlmPortfolioDecision.review_date.desc())
        .first()
    )


def test_a_hallucinated_ticker_does_not_fail_an_otherwise_usable_cycle():
    """Production showed 16 'failed' runs in 11 trading days for this reason.

    ``decide_trades`` returns per-decision validation errors alongside the
    decisions it accepted. Treating any error as a cycle failure condemned runs
    that worked — and because "failed" is not terminal, every later trigger
    that day re-ran the whole cycle.
    """
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "hold", "target_weight": 0.0,
             "thesis": "", "confidence": 0.4},
            {"ticker": "GHOST", "action": "buy", "target_weight": 0.1,
             "thesis": "Not in the allowed set.", "confidence": 0.9},
        ]),
    )

    assert result["status"] == "no_trades"
    assert _cycle_row(db).status == "idle_no_trades"
    # The validation error is still recorded — downgraded, not swallowed.
    assert any("GHOST" in e for e in result["errors"])


def test_a_response_with_nothing_usable_is_still_a_failure():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "GHOST", "action": "buy", "target_weight": 0.1,
             "thesis": "Not in the allowed set.", "confidence": 0.9},
        ]),
    )

    assert result["status"] == "failed"
    assert _cycle_row(db).status == "failed"


def test_failed_cycles_are_retried_but_not_without_bound():
    """Two schedulers plus manual runs each re-ran a failed day indefinitely."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    assert not _already_ran_today(db, portfolio.id)
    for _ in range(_MAX_FAILED_ATTEMPTS_PER_DAY - 1):
        db.add(
            LlmPortfolioDecision(
                portfolio_id=portfolio.id, review_date=datetime.now(UTC),
                mandate=ADVISOR_MANDATE, decision_json="{}", status="failed",
            )
        )
    db.commit()
    assert not _already_ran_today(db, portfolio.id), "retries must still be allowed"

    db.add(
        LlmPortfolioDecision(
            portfolio_id=portfolio.id, review_date=datetime.now(UTC),
            mandate=ADVISOR_MANDATE, decision_json="{}", status="failed",
        )
    )
    db.commit()
    assert _already_ran_today(db, portfolio.id), "the cap must stop the retry storm"


def test_a_terminal_status_still_short_circuits_the_day():
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    portfolio = seed_paper_portfolio_from_real(db, user.id)

    db.add(
        LlmPortfolioDecision(
            portfolio_id=portfolio.id, review_date=datetime.now(UTC),
            mandate=ADVISOR_MANDATE, decision_json="{}", status="idle_no_trades",
        )
    )
    db.commit()
    assert _already_ran_today(db, portfolio.id)


def test_cycle_status_follows_the_book_not_just_model_named_orders():
    """The trade log and the cycle log must never contradict each other.

    ``_raise_cash`` books real sells, but they were never added to ``executed``
    — only orders the *model* named were — and the status was derived from
    ``executed``. Any cycle whose fills were all funding sells would therefore
    record ``idle_no_trades`` over a changed book, which is the class of
    contradiction that made "1 trade in 20 days" impossible to reconcile against
    a cycle history showing completed runs. The status is now read off the
    book itself, so the invariant holds however the fills arose.
    """
    db = _memory_db()
    user = _user(db)
    # 0.11 of NAV leaves the trim room inside the turnover cap, so this cycle
    # fills a funding sell as well as the buy it financed.
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.11,
             "thesis": "High conviction.", "confidence": 0.9},
        ]),
    )

    row = _cycle_row(db)
    assert result["funding_sells"], "expected the trim that funds the buy"
    assert bool(result["trades"]) is (row.status == "completed")
    # The audit row carries the endpoint's account of the decision call, so a
    # failure can be diagnosed from the record instead of reproduced.
    assert "llm_attempts" in json.loads(row.decision_json)


def test_a_cycle_that_filled_nothing_is_still_not_completed():
    """The other half of the invariant: no fills, no 'completed'."""
    db = _memory_db()
    user = _user(db)
    _seed_fully_invested_book(db, user, cash=20.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly(100.0, drift=0.0))
    _seed_prices(db, "CAND", _wiggly(40.0))

    result = run_advisor_cycle(
        db, user.id,
        # Larger than the turnover cap can fund, so nothing clears.
        llm_call=_stub_llm([
            {"ticker": "CAND", "action": "buy", "target_weight": 0.5,
             "thesis": "All in.", "confidence": 0.9},
        ]),
    )

    assert result["trades"] == []
    assert _cycle_row(db).status == "idle_no_trades"
