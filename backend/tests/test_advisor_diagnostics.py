"""Tests for the advisor-loop diagnostics health check.

These cover the failure modes that actually happened in production and were
invisible: a starved candidate feed, silent cycle days with no audit row, a
sleeve that cannot afford the trades it proposes, a scorecard withholding its
composite, and a learning loop that has never produced a lesson.
"""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorScorecard,
    AdvisorStrategy,
    DiscoverCandidate,
    DiscoverRun,
    DiscoveryPrediction,
    JobRun,
    LlmPortfolioDecision,
    PaperHolding,
    PaperPortfolio,
    PaperTrade,
    PriceCache,
    StrategyLesson,
    User,
)
from app.decision.advisor.diagnostics import FAIL, OK, WARN, run_advisor_diagnostics


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _sleeve(db, user: User, mandate: str, *, cash: float = 50_000.0) -> PaperPortfolio:
    portfolio = PaperPortfolio(
        id=uuid4().hex,
        user_id=user.id,
        name=f"Sleeve {mandate}",
        initial_cash=Decimal(str(cash)),
        baseline_value=Decimal(str(cash)),
        mandate=mandate,
        managed_by="llm",
    )
    db.add(portfolio)
    db.commit()
    return portfolio


def _strategies(db, user: User, *, cash: float = 50_000.0) -> tuple[PaperPortfolio, PaperPortfolio]:
    champ_pf = _sleeve(db, user, "advisor", cash=cash)
    chall_pf = _sleeve(db, user, "advisor-challenger", cash=cash)
    db.add_all(
        [
            AdvisorStrategy(
                id=uuid4().hex, user_id=user.id, role="champion",
                portfolio_id=champ_pf.id, config_json={},
            ),
            AdvisorStrategy(
                id=uuid4().hex, user_id=user.id, role="challenger",
                portfolio_id=chall_pf.id, config_json={},
            ),
        ]
    )
    db.commit()
    return champ_pf, chall_pf


def _job(db, name: str, *, hours_ago: float, status: str = "success") -> None:
    started = datetime.now(UTC) - timedelta(hours=hours_ago)
    db.add(
        JobRun(
            id=uuid4().hex, job_name=name, status=status,
            started_at=started, finished_at=started, triggered_by="scheduler",
        )
    )
    db.commit()


_LOOP_JOB_NAMES = (
    "discover_refresh",
    "advisor_cycle",
    "evolution_round",
    "snapshot_paper_portfolios",
    "discovery_resolution",
    "price_refresh",
)


def _healthy_jobs(db, *, exclude: tuple[str, ...] = ()) -> None:
    for name in _LOOP_JOB_NAMES:
        if name in exclude:
            continue
        _job(db, name, hours_ago=1.0)


def _discover_run(db, user: User, symbols: list[str], *, days_ago: float = 0.5) -> DiscoverRun:
    run = DiscoverRun(
        id=uuid4().hex, user_id=user.id, status="completed",
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
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


def _seed_prices(db, ticker: str, n: int = 120, close: float = 100.0) -> None:
    day = date.today()
    now = datetime.now(UTC)
    added = 0
    while added < n:
        if day.weekday() < 5:
            db.add(
                PriceCache(
                    id=uuid4().hex, ticker=ticker.upper(), date=day,
                    close=Decimal(str(close)), fetched_at=now,
                    source="test", stale=False, currency="EUR",
                )
            )
            added += 1
        day -= timedelta(days=1)
    db.commit()


def _check(report: dict, key: str) -> dict:
    return next(c for c in report["checks"] if c["key"] == key)


def _healthy_cycles(
    db,
    portfolio: PaperPortfolio,
    *,
    days: int = 15,
    payload: dict | None = None,
) -> None:
    """One non-failing cycle record per trading day, so the stages downstream of
    cycle history are judged on their own state rather than on a broken loop."""
    now = datetime.now(UTC)
    for offset in range(days):
        day = now - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=portfolio.id, review_date=day,
                mandate=portfolio.mandate, decision_json=json.dumps(payload or {}),
                status="idle_no_trades",
            )
        )
    db.commit()


def test_no_sleeve_reports_missing_champion() -> None:
    db = _memory_db()
    user = _user(db)

    report = run_advisor_diagnostics(db, user.id)

    assert report["status"] == FAIL
    assert _check(report, "sleeves")["status"] == FAIL
    assert report["blocking"], "a report with no sleeve must list blocking stages"


def test_missing_discover_run_is_the_first_blocking_stage() -> None:
    """No completed Discover run means the cycle exits with no_candidates daily."""
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)
    _healthy_jobs(db)

    report = run_advisor_diagnostics(db, user.id)

    feed = _check(report, "candidate_feed")
    assert feed["status"] == FAIL
    assert "no_candidates" in feed["detail"]
    # Pipeline order: the feed is reported ahead of the stages it starves, so
    # the first blocking entry is the thing to fix.
    blocking_keys = [b["key"] for b in report["blocking"]]
    assert blocking_keys[0] == "candidate_feed"
    # ...and trade flow no longer claims to be an independent blocker, which is
    # what turned one root cause into several red stages.
    assert "trade_activity" not in blocking_keys

    # The starved stage says whose fault it is.
    trades = _check(report, "trade_activity")
    assert trades["status"] == WARN
    assert trades["data"]["upstream_blocker"] == feed["label"]
    assert feed["label"] in trades["detail"]


def test_stale_discover_run_warns_about_frozen_targets() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)
    _healthy_jobs(db)
    _discover_run(db, user, ["AAA"], days_ago=21.0)

    feed = _check(run_advisor_diagnostics(db, user.id), "candidate_feed")

    assert feed["status"] == WARN
    assert feed["data"]["latest_run"]["age_days"] >= 20
    assert "already met" in feed["detail"]


def test_scheduler_flags_a_worker_that_never_ran() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    check = _check(run_advisor_diagnostics(db, user.id), "scheduler")

    assert check["status"] == FAIL
    assert "never run" in check["detail"] or "never ran" in check["detail"]


def test_scheduler_flags_a_stale_job() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)
    _healthy_jobs(db, exclude=("advisor_cycle",))
    _job(db, "advisor_cycle", hours_ago=240.0)

    check = _check(run_advisor_diagnostics(db, user.id), "scheduler")

    assert check["status"] in (WARN, FAIL)
    assert "advisor_cycle" in check["detail"]


def test_silent_cycle_days_are_detected() -> None:
    """A trading day with no decision row at all is the silent-failure signature."""
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    _discover_run(db, user, ["AAA"])
    # One record 20 days ago, nothing since — the production symptom.
    db.add(
        LlmPortfolioDecision(
            id=uuid4().hex, portfolio_id=champ.id,
            review_date=datetime.now(UTC) - timedelta(days=20),
            mandate="advisor", decision_json="{}", status="completed",
        )
    )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    assert check["status"] == FAIL
    assert check["data"]["days_with_a_record"] == 0
    assert len(check["data"]["silent_days"]) >= 8


def test_cycle_history_clean_when_every_trading_day_has_a_record() -> None:
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)

    now = datetime.now(UTC)
    for offset in range(0, 15):
        day = now - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=champ.id, review_date=day,
                mandate="advisor", decision_json="{}", status="idle_no_trades",
            )
        )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    assert check["status"] == OK
    assert check["data"]["silent_days"] == []


def test_cycle_history_counts_a_record_on_the_windows_oldest_day() -> None:
    """A record stamped early on the oldest day in the window still counts.

    The query filtered on the raw ``ref - 14d`` timestamp while the expected-day
    set counted the whole date, so a record written earlier that same day was
    excluded and the day reported silent. It only surfaced when the boundary day
    fell on a weekday, which made it look like a Monday-only flake.
    """
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)

    now = datetime.now(UTC)
    for offset in range(0, 15):
        day = now - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=champ.id,
                # Start of day — earlier than ref-14d on the boundary date.
                review_date=day.replace(hour=0, minute=1, second=0, microsecond=0),
                mandate="advisor", decision_json="{}", status="idle_no_trades",
            )
        )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    assert check["data"]["silent_days"] == []
    assert check["status"] == OK


def test_low_cash_is_healthy_when_holdings_can_fund_the_trade() -> None:
    """The real-world shape: a fully-invested mirror with almost no cash.

    Buys are financed by the sells the model picks in the same cycle, so this
    is the expected steady state — not a fault.
    """
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user, cash=200.0)
    _healthy_jobs(db)
    db.add(
        PaperHolding(
            id=uuid4().hex, portfolio_id=champ.id, ticker="HELD", name="Held",
            quantity=Decimal("500"), avg_buy_price=Decimal("100"), asset_type="stock",
        )
    )
    db.commit()
    _seed_prices(db, "HELD")

    check = _check(run_advisor_diagnostics(db, user.id), "cash_headroom")

    assert check["status"] == OK
    assert check["data"]["can_fund_a_trade"] is True
    assert check["data"]["sellable_securities"] > 0
    assert "financed by sells" in check["detail"]


def test_trade_funding_fails_on_an_empty_sleeve() -> None:
    """No cash and no holdings — the only genuinely unfundable case.

    Once the sleeve holds anything at all, a buy can be financed by selling it,
    so a low cash balance on its own is never the blocker.
    """
    db = _memory_db()
    user = _user(db)
    _strategies(db, user, cash=0.0)
    _healthy_jobs(db)

    check = _check(run_advisor_diagnostics(db, user.id), "cash_headroom")

    assert check["status"] == FAIL
    assert check["data"]["can_fund_a_trade"] is False


def test_scorecard_explains_the_insufficient_data_label() -> None:
    """Composite is withheld below 3 computable axes — the UI's "insufficient data"."""
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    today = date.today()
    db.add(
        AdvisorScorecard(
            id=uuid4().hex, user_id=user.id, portfolio_id=champ.id,
            window_start=today - timedelta(days=90), window_end=today,
            n_predictions=1, n_resolved=0,
            sharpe=0.4, max_drawdown=0.05,  # only the two NAV axes
        )
    )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "scorecard")

    assert check["status"] == FAIL
    assert check["data"]["composite"] is None
    assert check["data"]["axes_computable_count"] == 2
    assert "insufficient data" in check["detail"]
    assert any("calibration" in r for r in check["data"]["blocking_reasons"])


def test_scorecard_ok_once_all_four_axes_compute() -> None:
    """F15: composite requires magnitude specifically, not just any 3 of 4 —
    without mz_slope/mz_r2 this would previously have silently computed a
    composite from risk_adjusted/calibration/downside alone."""
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    today = date.today()
    db.add(
        AdvisorScorecard(
            id=uuid4().hex, user_id=user.id, portfolio_id=champ.id,
            window_start=today - timedelta(days=90), window_end=today,
            n_predictions=25, n_resolved=25,
            sharpe=0.4, max_drawdown=0.05, brier_avg=0.18,
            mz_slope=0.9, mz_r2=0.6,
        )
    )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "scorecard")

    assert check["status"] == OK
    assert check["data"]["composite"] is not None


def test_scorecard_withheld_when_magnitude_missing_despite_other_three_axes() -> None:
    """The exact prod bug (F15): three strong axes with no magnitude signal
    must NOT produce a composite — the scorecard check must report
    "insufficient data", not OK."""
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    today = date.today()
    db.add(
        AdvisorScorecard(
            id=uuid4().hex, user_id=user.id, portfolio_id=champ.id,
            window_start=today - timedelta(days=90), window_end=today,
            n_predictions=25, n_resolved=25,
            sharpe=0.4, max_drawdown=0.05, brier_avg=0.18,
            # mz_slope/mz_r2 intentionally left unset — no magnitude signal.
        )
    )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "scorecard")

    assert check["status"] == FAIL
    assert check["data"]["composite"] is None
    assert "magnitude" in check["detail"]


def test_learning_loop_reports_zero_lessons_ever_written() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)
    _healthy_jobs(db)

    check = _check(run_advisor_diagnostics(db, user.id), "learning")

    assert check["status"] == FAIL
    assert check["data"]["lessons_total"] == 0
    assert check["data"]["promotion_sample_shortfall"] == 20
    assert "empty 'lessons' array" in check["detail"]


def test_learning_loop_ok_with_active_lessons_and_sample() -> None:
    db = _memory_db()
    user = _user(db)
    champ, chall = _strategies(db, user)
    _healthy_jobs(db)
    strategies = db.query(AdvisorStrategy).filter(AdvisorStrategy.user_id == user.id).all()
    for strategy in strategies:
        db.add(
            StrategyLesson(
                id=uuid4().hex, user_id=user.id, strategy_id=strategy.id,
                portfolio_id=strategy.portfolio_id,
                lesson_text="Trim momentum names into strength.", rank=1.0, active=True,
            )
        )
        for _ in range(20):
            db.add(
                DiscoveryPrediction(
                    id=uuid4().hex, user_id=user.id, portfolio_id=strategy.portfolio_id,
                    symbol="AAA", run_id="r", conviction=0.6, horizon_days=21,
                    predicted_at=datetime.now(UTC) - timedelta(days=40),
                    resolve_at=datetime.now(UTC) - timedelta(days=5),
                    outcome_status="resolved", realised_return=0.03,
                    price_at_prediction=100.0,
                )
            )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "learning")

    assert check["status"] == OK
    assert check["data"]["promotion_sample_shortfall"] == 0


def test_prediction_ledger_flags_overdue_pending_rows() -> None:
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    db.add(
        DiscoveryPrediction(
            id=uuid4().hex, user_id=user.id, portfolio_id=champ.id,
            symbol="AAA", run_id="r", conviction=0.6, horizon_days=21,
            predicted_at=datetime.now(UTC) - timedelta(days=60),
            resolve_at=datetime.now(UTC) - timedelta(days=10),
            outcome_status="pending", price_at_prediction=100.0,
        )
    )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "prediction_ledger")

    assert check["status"] == WARN
    assert check["data"]["overdue_pending"] == 1


def test_price_history_flags_candidates_with_no_cached_closes() -> None:
    """Discover candidates are not covered by the price backfill job."""
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)
    _healthy_jobs(db)
    _discover_run(db, user, ["HASDATA", "BARE1", "BARE2"])
    _seed_prices(db, "HASDATA")

    check = _check(run_advisor_diagnostics(db, user.id), "price_history")

    assert check["status"] == FAIL  # only 1 evaluable, optimiser needs ≥2
    assert set(check["data"]["not_evaluable"]) == {"BARE1", "BARE2"}


def test_trade_activity_reports_days_since_last_trade() -> None:
    """A stale book with nothing broken upstream is the sleeve's own problem."""
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    _discover_run(db, user, ["AAA", "BBB"])
    _seed_prices(db, "AAA")
    _seed_prices(db, "BBB")
    _healthy_cycles(
        db,
        champ,
        payload={"skipped": [{"ticker": "AAA", "action": "buy", "reason": "target weight already met"}]},
    )
    db.add(
        PaperTrade(
            id=uuid4().hex, portfolio_id=champ.id, ticker="AAA", side="buy",
            quantity=Decimal("10"), price=Decimal("100"), value=Decimal("1000"),
            date=datetime.now(UTC) - timedelta(days=21),
        )
    )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "trade_activity")

    assert check["status"] == FAIL
    assert check["data"]["total_trades"] == 1
    assert check["data"]["days_since_last_trade"] >= 20
    assert check["data"]["upstream_blocker"] is None
    # The binding constraint is named, from the cycle's own audit row.
    assert check["data"]["latest_cycle_skip_reasons"] == {"target weight already met": 1}
    assert "target weight already met" in check["detail"]


def test_llm_probe_detects_reasoning_leak() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    def _thinking_llm(_db, _messages) -> str:
        return "<think>Let me consider the candidates carefully...</think>"

    report = run_advisor_diagnostics(db, user.id, probe_llm=True, llm_call=_thinking_llm)
    check = _check(report, "llm")

    assert check["status"] == FAIL
    assert check["data"]["has_reasoning_leak"] is True
    assert "reasoning_effort" in (check["remedy"] or "")


def test_llm_probe_flags_unparseable_output() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    report = run_advisor_diagnostics(
        db, user.id, probe_llm=True, llm_call=lambda _db, _m: "Sure! Here are my picks."
    )
    check = _check(report, "llm")

    assert check["status"] == FAIL
    assert check["data"]["parsed_json"] is False


def test_llm_probe_passes_when_the_model_funds_the_buy_with_a_sell() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    payload = json.dumps({"decisions": [
        {"ticker": "OVERWEIGHT.AS", "thesis": "trim the concentration",
         "action": "sell", "target_weight": 0.5, "confidence": 0.7},
        {"ticker": "ASML.AS", "thesis": "fund from the trim",
         "action": "buy", "target_weight": 0.4, "confidence": 0.6},
    ]})
    report = run_advisor_diagnostics(db, user.id, probe_llm=True, llm_call=lambda _db, _m: payload)
    check = _check(report, "llm")

    assert check["status"] == OK
    assert check["data"]["probe_proposed_sell"] is True


def test_llm_probe_warns_when_schema_valid_json_contains_no_sell() -> None:
    """A parseable answer is not a working decision stage.

    The old probe asked one ticker against an empty book, so 'parsed_json: true'
    was fully consistent with a sleeve that had never proposed a sell.
    """
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    payload = json.dumps(
        {"decisions": [{"ticker": "ASML.AS", "thesis": "ok", "action": "buy",
                        "target_weight": 0.08, "confidence": 0.7}]}
    )
    report = run_advisor_diagnostics(db, user.id, probe_llm=True, llm_call=lambda _db, _m: payload)
    check = _check(report, "llm")

    assert check["status"] == WARN
    assert check["data"]["parsed_json"] is True
    assert check["data"]["probe_proposed_sell"] is False
    assert "no sell" in check["detail"]


def test_llm_is_skipped_unless_probed() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    check = _check(run_advisor_diagnostics(db, user.id), "llm")

    assert check["status"] == "skipped"
    assert check["data"]["probed"] is False


def test_report_shape_is_stable() -> None:
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    report = run_advisor_diagnostics(db, user.id)

    assert set(report) == {
        "status", "generated_at", "user_id", "summary", "counts", "blocking", "pending",
        "checks",
    }
    for check in report["checks"]:
        assert set(check) == {"key", "label", "status", "detail", "data", "remedy"}
        assert check["status"] in (OK, WARN, FAIL, "skipped")


def test_cycle_history_names_the_reason_a_run_failed() -> None:
    """16 failed runs were reported as a bare status tally with no reason.

    The newest record is usually a healthy one, so ``last_record`` showed a
    clean row while the failures stayed invisible — the panel said "a record
    exists for every trading day" and the user had nowhere to look.
    """
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)

    now = datetime.now(UTC)
    for offset in range(0, 15):
        day = now - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        failed = offset > 0
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=champ.id, review_date=day,
                mandate="advisor", decision_json="{}",
                status="failed" if failed else "idle_no_trades",
                error="decision[0] (GHOST) outside risk-gate-passing set" if failed else None,
            )
        )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    # A window that is mostly failures blocks the loop; it is not a warning.
    assert check["status"] == FAIL
    assert "GHOST" in check["detail"]
    assert check["data"]["last_failure"]["error"].startswith("decision[0]")
    assert check["remedy"]


def test_cycle_history_warns_when_failures_are_the_minority() -> None:
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)

    now = datetime.now(UTC)
    trading_day_index = 0
    for offset in range(0, 15):
        day = now - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        # Key the failed day off a trading-day counter, not the raw calendar
        # offset — an offset that happens to land on a weekend is skipped by
        # the guard above, silently dropping the failure and making this test
        # pass or fail depending on which weekday "now" is.
        is_failed_day = trading_day_index == 3
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=champ.id, review_date=day,
                mandate="advisor", decision_json="{}",
                status="failed" if is_failed_day else "idle_no_trades",
                error="LLM response was not valid JSON" if is_failed_day else None,
            )
        )
        trading_day_index += 1
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    assert check["status"] == WARN
    assert "not valid JSON" in check["detail"]


def test_cycle_history_counts_days_not_retry_attempts() -> None:
    """A failed cycle is retried by every trigger that day, so rows over-count.

    Production showed "16 of 19 runs failed" across 11 trading days: the 10:00
    advisor job, the 10:30 evolution round and manual runs each wrote their own
    failed row for the same day. Six bad days read as sixteen failures, a number
    that maps onto nothing the reader can act on.
    """
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)

    now = datetime.now(UTC)
    bad_days = 0
    for offset in range(0, 15):
        day = now - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        # The most recent trading days are healthy; everything older failed
        # three times over, once per trigger.
        attempts = 1 if offset <= 4 else 3
        status = "idle_no_trades" if offset <= 4 else "failed"
        if status == "failed":
            bad_days += 1
        for attempt in range(attempts):
            db.add(
                LlmPortfolioDecision(
                    id=uuid4().hex, portfolio_id=champ.id,
                    review_date=day - timedelta(minutes=attempt),
                    mandate="advisor", decision_json="{}", status=status,
                    error="LLM response was not valid JSON" if status == "failed" else None,
                )
            )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    assert len(check["data"]["failed_days"]) == bad_days
    assert check["data"]["total_runs"] > check["data"]["days_with_a_record"]
    assert max(check["data"]["attempts_per_day"].values()) == 3
    # Every failure is older than the recency window, so the loop has recovered.
    assert check["data"]["recent_failed_days"] == []
    assert check["status"] == WARN
    assert "most recent cycles are healthy" in check["detail"]


def test_cycle_history_surfaces_the_truncated_completion_behind_a_failure() -> None:
    """'not valid JSON' has two causes with opposite fixes.

    A grammar-constrained call cannot emit malformed JSON, so a parse failure
    usually means the completion was cut off at the token limit. The cycle
    already stores the endpoint's finish_reason; the panel just never read it.
    """
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)

    now = datetime.now(UTC)
    for offset in range(0, 15):
        day = now - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=champ.id, review_date=day,
                mandate="advisor", status="failed",
                error="LLM response was not valid JSON",
                decision_json=json.dumps({
                    "llm_attempts": [
                        {"attempt": 1, "finish_reason": "length", "truncated": True,
                         "max_tokens": 8192, "completion_tokens": 8192},
                    ],
                    "llm_raw_responses": ['{"decisions": [{"ticker": "AAA", "thesis": "'],
                }),
            )
        )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    assert check["status"] == FAIL
    failure = check["data"]["last_failure"]
    assert failure["llm_attempts"][0]["finish_reason"] == "length"
    assert "cut off" in failure["diagnosis"]
    assert "cut off" in check["detail"]
    assert "finish_reason" in (check["remedy"] or "")


def test_cycle_history_ignores_rows_from_another_mandate() -> None:
    """The weekly mandate review writes decision rows against the same sleeve."""
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    _healthy_cycles(db, champ)

    now = datetime.now(UTC)
    for offset in range(0, 5):
        db.add(
            LlmPortfolioDecision(
                id=uuid4().hex, portfolio_id=champ.id,
                review_date=now - timedelta(days=offset),
                mandate="mirror", decision_json="{}", status="failed",
                error="unrelated weekly review failure",
            )
        )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "cycle_history")

    assert check["status"] == OK
    assert check["data"]["mandate"] == "advisor"
    assert "failed" not in check["data"]["status_counts"]


def test_scorecard_is_pending_not_broken_while_predictions_mature() -> None:
    """Two missing axes on day one are the calendar, not a defect."""
    db = _memory_db()
    user = _user(db)
    champ, _ = _strategies(db, user)
    _healthy_jobs(db)
    today = date.today()
    db.add(
        AdvisorScorecard(
            id=uuid4().hex, user_id=user.id, portfolio_id=champ.id,
            window_start=today - timedelta(days=90), window_end=today,
            n_predictions=3, n_resolved=0,
            sharpe=0.4, max_drawdown=0.05,  # only the two NAV axes
        )
    )
    db.add(
        DiscoveryPrediction(
            id=uuid4().hex, user_id=user.id, portfolio_id=champ.id, symbol="AAA",
            run_id="r1", direction="buy", conviction=0.6, outcome_status="pending",
            horizon_days=21,
            predicted_at=datetime.now(UTC) - timedelta(days=2),
            resolve_at=datetime.now(UTC) + timedelta(days=9),
        )
    )
    db.commit()

    report = run_advisor_diagnostics(db, user.id)
    check = _check(report, "scorecard")

    assert check["status"] == WARN
    assert check["data"]["waiting_on_horizon"] is True
    assert check["data"]["days_until_next_resolution"] > 0
    assert "Not a fault" in check["detail"]
    # A stage waiting on the calendar is never listed as a blocker.
    assert "scorecard" not in [b["key"] for b in report["blocking"]]
    assert "scorecard" in [p["key"] for p in report["pending"]]
    assert "waiting on predictions" in report["summary"]


def test_learning_loop_is_pending_while_the_first_prediction_matures() -> None:
    db = _memory_db()
    user = _user(db)
    champ, chall = _strategies(db, user)
    _healthy_jobs(db)
    for sleeve in (champ, chall):
        db.add(
            DiscoveryPrediction(
                id=uuid4().hex, user_id=user.id, portfolio_id=sleeve.id, symbol="AAA",
                run_id="r1", direction="buy", conviction=0.6, outcome_status="pending",
                horizon_days=21,
                predicted_at=datetime.now(UTC) - timedelta(days=1),
                resolve_at=datetime.now(UTC) + timedelta(days=14),
            )
        )
    db.commit()

    check = _check(run_advisor_diagnostics(db, user.id), "learning")

    assert check["status"] == WARN
    assert check["data"]["waiting_on_horizon"] is True
    assert "Not a fault yet" in check["detail"]


def test_learning_loop_fails_when_nothing_will_ever_resolve() -> None:
    """No lessons and no pending predictions is a real dead end."""
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)
    _healthy_jobs(db)

    check = _check(run_advisor_diagnostics(db, user.id), "learning")

    assert check["status"] == FAIL
    assert "nothing will ever resolve" in check["detail"]


def test_llm_probe_flags_a_completion_cut_off_at_the_token_limit() -> None:
    """The probe must fail on truncation, not report parseable-but-short JSON.

    A grammar cannot produce malformed JSON, so a real cycle's "not valid JSON"
    is nearly always a completion that ran out of budget. The probe reported OK
    through this because it never looked at finish_reason.
    """
    db = _memory_db()
    user = _user(db)
    _strategies(db, user)

    def _truncating_llm(_db, _messages, **kwargs) -> str:
        telemetry = kwargs.get("telemetry")
        if telemetry is not None:
            telemetry.update({"finish_reason": "length", "truncated": True, "max_tokens": 792})
        return '{"decisions": [{"ticker": "ASML.AS", "thesis": "the model kept writing'

    report = run_advisor_diagnostics(db, user.id, probe_llm=True, llm_call=_truncating_llm)
    check = _check(report, "llm")

    assert check["status"] == FAIL
    assert check["data"]["truncated"] is True
    assert "finish_reason=length" in check["detail"]
    assert "thesis" in (check["remedy"] or "")
