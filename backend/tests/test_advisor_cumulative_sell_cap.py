"""Tests for the advisor cycle's trailing-window cumulative sell cap and
core-holding floor (ADR 0013, follow-up to ADR 0012's per-cycle sell cap).

ADR 0012's own historical replay found the per-cycle cap insufficient: many
small, individually-compliant sells of the same position compounded across
weeks into a much larger cumulative drawdown than any single cycle's cap
would allow. These tests cover the trailing-window cumulative cap and the
core-holding (ETF) floor that close that gap.
"""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    DiscoverRun,
    DiscoverCandidate,
    PaperHolding,
    PaperPortfolio,
    PaperTrade,
    PriceCache,
    User,
)
from app.decision.advisor.cycle import (
    DEFAULT_CORE_HOLDING_FLOOR_PCT,
    DEFAULT_CUMULATIVE_SELL_WINDOW_DAYS,
    DEFAULT_MAX_CUMULATIVE_SELL_PCT,
    DEFAULT_MAX_SELL_PCT_OF_POSITION,
    _cumulative_sell_totals,
    _effective_sell_cap,
    _plan_decision,
    run_advisor_cycle,
)
from app.decision.advisor.decision import McSummary, TradeProposal
from app.decision.advisor.llm_decision import TradeDecision, _build_user_prompt
from app.decision.advisor.risk_gate import check_risk_gate
from app.decision.paper_portfolio import seed_paper_portfolio_from_real


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _proposal(book: dict[str, float], instrument_types: dict[str, str] | None = None) -> TradeProposal:
    return TradeProposal(
        portfolio_id="p1",
        suggested_weights={sym: 0.0 for sym in book},
        mc_summaries={
            sym: McSummary(sym, (instrument_types or {}).get(sym, "equity"), -0.05, 0.01, 0.08, 0.6, 100.0)
            for sym in book
        },
        risk_envelope={},
        current_book=book,
        optimizer_status="completed",
        instrument_types=instrument_types or {},
    )


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


def _wiggly(base: float, n: int = 300, drift: float = 0.0) -> list[float]:
    return [
        base * (1 + i * drift)
        + base * (0.005 if i % 7 == 0 else (-0.004 if i % 5 == 0 else 0.0))
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# _cumulative_sell_totals
# ---------------------------------------------------------------------------


def _make_portfolio(db, user) -> PaperPortfolio:
    portfolio = PaperPortfolio(
        id=uuid4().hex, user_id=user.id, currency="EUR",
        initial_cash=Decimal("100000"), mandate="advisor",
    )
    db.add(portfolio)
    db.commit()
    return portfolio


def _add_trade(db, portfolio_id, ticker, side, value, *, days_ago=0):
    db.add(
        PaperTrade(
            id=uuid4().hex, portfolio_id=portfolio_id, ticker=ticker, side=side,
            quantity=Decimal("1"), price=Decimal("1"), value=Decimal(str(value)),
            date=datetime.now(UTC) - timedelta(days=days_ago),
        )
    )
    db.commit()


def test_cumulative_sell_totals_sums_sells_and_buys_within_window():
    db = _memory_db()
    user = _user(db)
    portfolio = _make_portfolio(db, user)
    _add_trade(db, portfolio.id, "AAA", "sell", 1000.0, days_ago=5)
    _add_trade(db, portfolio.id, "AAA", "sell", 500.0, days_ago=10)
    _add_trade(db, portfolio.id, "AAA", "buy", 200.0, days_ago=2)
    _add_trade(db, portfolio.id, "BBB", "sell", 999.0, days_ago=1)

    totals = _cumulative_sell_totals(db, portfolio.id, window_days=28)

    assert totals["AAA"] == (1500.0, 200.0)
    assert totals["BBB"] == (999.0, 0.0)


def test_cumulative_sell_totals_excludes_trades_outside_the_window():
    db = _memory_db()
    user = _user(db)
    portfolio = _make_portfolio(db, user)
    _add_trade(db, portfolio.id, "AAA", "sell", 1000.0, days_ago=5)
    _add_trade(db, portfolio.id, "AAA", "sell", 5000.0, days_ago=40)  # older than 28d window

    totals = _cumulative_sell_totals(db, portfolio.id, window_days=28)

    assert totals["AAA"] == (1000.0, 0.0)


def test_cumulative_sell_totals_empty_when_no_trades():
    db = _memory_db()
    user = _user(db)
    portfolio = _make_portfolio(db, user)

    totals = _cumulative_sell_totals(db, portfolio.id, window_days=28)

    assert totals == {}


# ---------------------------------------------------------------------------
# _effective_sell_cap
# ---------------------------------------------------------------------------


def test_effective_cap_is_the_per_cycle_cap_with_no_prior_sells():
    cap = _effective_sell_cap(
        "AAA", 30_000.0, 60_000.0,
        max_sell_pct_of_position=DEFAULT_MAX_SELL_PCT_OF_POSITION,
        cumulative_totals={},
        max_cumulative_sell_pct=DEFAULT_MAX_CUMULATIVE_SELL_PCT,
        instrument_types={},
        core_holding_floor_pct=DEFAULT_CORE_HOLDING_FLOOR_PCT,
    )
    assert cap == 10_000.0  # 1/3 of 30,000


def test_cumulative_cap_binds_after_prior_sells_in_window():
    # Window-start value ≈ 20,000 (current) + 8,000 (already sold) = 28,000.
    # Cumulative cap = 28,000 / 3 ≈ 9,333.33; already sold 8,000 → ~1,333.33 left.
    cap = _effective_sell_cap(
        "AAA", 20_000.0, 60_000.0,
        max_sell_pct_of_position=DEFAULT_MAX_SELL_PCT_OF_POSITION,
        cumulative_totals={"AAA": (8_000.0, 0.0)},
        max_cumulative_sell_pct=DEFAULT_MAX_CUMULATIVE_SELL_PCT,
        instrument_types={},
        core_holding_floor_pct=DEFAULT_CORE_HOLDING_FLOOR_PCT,
    )
    per_cycle_cap = 20_000.0 / 3  # ≈ 6,666.67 — looser than the cumulative remainder
    assert cap < per_cycle_cap
    assert round(cap, 2) == 1_333.33


def test_cumulative_cap_fully_exhausted_blocks_further_sells():
    cap = _effective_sell_cap(
        "AAA", 20_000.0, 60_000.0,
        max_sell_pct_of_position=DEFAULT_MAX_SELL_PCT_OF_POSITION,
        cumulative_totals={"AAA": (12_373.0, 0.0)},  # already at/near the cumulative cap
        max_cumulative_sell_pct=DEFAULT_MAX_CUMULATIVE_SELL_PCT,
        instrument_types={},
        core_holding_floor_pct=DEFAULT_CORE_HOLDING_FLOOR_PCT,
    )
    assert cap == 0.0


def test_core_holding_floor_binds_for_etf_below_floor_headroom():
    # 20% floor of 100,000 NAV = 20,000. Position at 22,000 → only 2,000 sellable
    # before hitting the floor, tighter than the 1/3-of-position cap (~7,333).
    cap = _effective_sell_cap(
        "IWDA.L", 22_000.0, 100_000.0,
        max_sell_pct_of_position=DEFAULT_MAX_SELL_PCT_OF_POSITION,
        cumulative_totals={},
        max_cumulative_sell_pct=DEFAULT_MAX_CUMULATIVE_SELL_PCT,
        instrument_types={"IWDA.L": "etf"},
        core_holding_floor_pct=DEFAULT_CORE_HOLDING_FLOOR_PCT,
    )
    assert cap == 2_000.0


def test_core_holding_floor_does_not_apply_to_single_stock_positions():
    cap = _effective_sell_cap(
        "SNGL", 22_000.0, 100_000.0,
        max_sell_pct_of_position=DEFAULT_MAX_SELL_PCT_OF_POSITION,
        cumulative_totals={},
        max_cumulative_sell_pct=DEFAULT_MAX_CUMULATIVE_SELL_PCT,
        instrument_types={"SNGL": "stock"},
        core_holding_floor_pct=DEFAULT_CORE_HOLDING_FLOOR_PCT,
    )
    assert cap == 22_000.0 / 3  # only the per-cycle cap applies, not the floor


def test_core_holding_floor_is_a_hard_block_when_already_at_or_below_floor():
    cap = _effective_sell_cap(
        "IWDA.L", 18_000.0, 100_000.0,  # already below the 20,000 floor
        max_sell_pct_of_position=DEFAULT_MAX_SELL_PCT_OF_POSITION,
        cumulative_totals={},
        max_cumulative_sell_pct=DEFAULT_MAX_CUMULATIVE_SELL_PCT,
        instrument_types={"IWDA.L": "etf"},
        core_holding_floor_pct=DEFAULT_CORE_HOLDING_FLOOR_PCT,
    )
    assert cap == 0.0


# ---------------------------------------------------------------------------
# _plan_decision integration with sell_caps
# ---------------------------------------------------------------------------


def test_plan_decision_uses_the_tightest_precomputed_sell_cap():
    decision = TradeDecision(
        ticker="AAA", action="sell", target_weight=0.0,
        thesis="Exit entirely.", confidence=0.9,
    )
    proposal = _proposal({"AAA": 30_000.0})

    planned, skip_reason = _plan_decision(
        decision, proposal, 60_000.0, 100.0,
        DEFAULT_MAX_SELL_PCT_OF_POSITION,
        {"AAA": 1_500.0},  # cumulative cap already mostly exhausted
    )

    assert skip_reason is None
    assert planned is not None
    assert planned.notional == 1_500.0


def test_plan_decision_falls_back_to_the_pct_cap_when_ticker_missing_from_sell_caps():
    decision = TradeDecision(
        ticker="AAA", action="sell", target_weight=0.0,
        thesis="Exit entirely.", confidence=0.9,
    )
    proposal = _proposal({"AAA": 30_000.0})

    planned, _ = _plan_decision(
        decision, proposal, 60_000.0, 100.0, DEFAULT_MAX_SELL_PCT_OF_POSITION, {},
    )

    assert planned is not None
    assert planned.notional == 10_000.0  # unaffected — falls back to the pct cap


# ---------------------------------------------------------------------------
# max_sellable_eur surfaced in the prompt matches the enforced cap
# ---------------------------------------------------------------------------


def test_prompt_max_sellable_eur_uses_the_per_ticker_override():
    proposal = _proposal({"AAA": 30_000.0}, {"AAA": "etf"})
    gate_result = check_risk_gate(proposal)

    prompt = _build_user_prompt(
        proposal, gate_result, [],
        total_value_eur=60_000.0,
        max_sell_pct_of_position=DEFAULT_MAX_SELL_PCT_OF_POSITION,
        max_sellable_eur_by_ticker={"AAA": 1_234.56},
    )
    payload = json.loads(prompt)
    candidate = next(c for c in payload["candidates"] if c["ticker"] == "AAA")
    assert candidate["max_sellable_eur"] == 1_234.56


# ---------------------------------------------------------------------------
# End-to-end: a fresh cycle respects sells already made in the trailing window
# ---------------------------------------------------------------------------


def test_cycle_clamps_a_sell_that_would_exceed_the_cumulative_window_cap():
    db = _memory_db()
    user = _user(db)
    portfolio = _make_portfolio(db, user)
    holding = PaperHolding(
        id=uuid4().hex, portfolio_id=portfolio.id, ticker="IWDA.L", name="iShares Core MSCI World",
        asset_type="etf", quantity=Decimal("250"), avg_buy_price=Decimal("100"),
    )
    db.add(holding)
    db.commit()
    _seed_prices(db, "IWDA.L", _wiggly(148.0))
    run = DiscoverRun(id=uuid4().hex, user_id=user.id, status="completed", created_at=datetime.now(UTC))
    db.add(run)
    db.commit()
    db.add(DiscoverCandidate(id=uuid4().hex, run_id=run.id, symbol="IWDA.L", source="screen_index", status="shortlisted"))
    db.commit()

    # Simulate prior cycles in the trailing window that already sold most of
    # the cumulative allowance (window-start value ≈ current 37,000 + prior
    # sells 12,000 ≈ 49,000; cumulative cap ≈ 16,333; ~12,000 already spent).
    current_value_approx = 250 * 148.0
    _add_trade(db, portfolio.id, "IWDA.L", "sell", 12_000.0, days_ago=5)

    result = run_advisor_cycle(
        db, user.id,
        llm_call=lambda _db, _msgs: json.dumps({
            "decisions": [
                {"ticker": "IWDA.L", "action": "sell", "target_weight": 0.0,
                 "thesis": "Rotate out.", "confidence": 0.9},
            ]
        }),
    )

    trades = [t for t in result["trades"] if t.get("ticker") == "IWDA.L"]
    assert trades, f"expected the sell to execute (clamped, not skipped); skipped={result['skipped']}"
    sold_value = float(trades[0]["value"])
    window_start_value = current_value_approx + 12_000.0
    cumulative_cap = window_start_value * DEFAULT_MAX_CUMULATIVE_SELL_PCT
    remaining_allowance = cumulative_cap - 12_000.0
    per_cycle_cap = current_value_approx * DEFAULT_MAX_SELL_PCT_OF_POSITION
    assert remaining_allowance < per_cycle_cap  # cumulative cap is the tighter, binding one here
    # The executed sell must respect the remaining cumulative allowance, not
    # just the (much looser) per-cycle 1/3-of-position cap.
    assert round(sold_value, 2) == round(remaining_allowance, 2)


# ---------------------------------------------------------------------------
# Config overrides
# ---------------------------------------------------------------------------


def test_cumulative_and_floor_config_are_recorded_in_the_audit_trail():
    db = _memory_db()
    user = _user(db)
    portfolio = _make_portfolio(db, user)
    holding = PaperHolding(
        id=uuid4().hex, portfolio_id=portfolio.id, ticker="AAA", name="Held",
        asset_type="stock", quantity=Decimal("100"), avg_buy_price=Decimal("100"),
    )
    db.add(holding)
    db.commit()
    _seed_prices(db, "AAA", _wiggly(100.0))
    run = DiscoverRun(id=uuid4().hex, user_id=user.id, status="completed", created_at=datetime.now(UTC))
    db.add(run)
    db.commit()
    db.add(DiscoverCandidate(id=uuid4().hex, run_id=run.id, symbol="AAA", source="screen_index", status="shortlisted"))
    db.commit()

    run_advisor_cycle(
        db, user.id,
        config={
            "max_cumulative_sell_pct": 0.5,
            "cumulative_sell_window_days": 10,
            "core_holding_floor_pct": 0.1,
        },
        llm_call=lambda _db, _msgs: json.dumps({"decisions": []}),
    )

    from app.foundation.models.entities import LlmPortfolioDecision
    row = (
        db.query(LlmPortfolioDecision)
        .filter(LlmPortfolioDecision.mandate == "advisor")
        .one()
    )
    flow = json.loads(row.decision_json)["trade_flow"]
    assert flow["max_cumulative_sell_pct"] == 0.5
    assert flow["cumulative_sell_window_days"] == 10
    assert flow["core_holding_floor_pct"] == 0.1
