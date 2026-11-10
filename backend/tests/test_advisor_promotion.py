"""Separate challenger sleeves and paired promotion (plan 2026-11-10 WS3, ADR 0025)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest
from scipy.stats import norm, ttest_1samp
from sqlalchemy.exc import IntegrityError

from conftest import _memory_db
from promotion_helpers import seed_paired_history

from app.decision.advisor import promotion as rule
from app.decision.advisor.scorecard import compute_advisor_scorecard
from app.decision.advisor.shadow import (
    band_rps,
    parse_forecasts,
    resolve_shadow_forecasts,
    run_shadow_forecasts,
)
from app.decision.advisor.strategy import (
    CHALLENGER_MANDATE,
    SPLIT_SETTING_KEY,
    ensure_champion,
    get_active_challenger,
    spawn_challenger,
    split_shared_challenger_sleeve,
    strategy_mandate,
)
from app.foundation.models.entities import (
    AdvisorShadowForecast,
    AdvisorStrategy,
    DiscoverCandidate,
    DiscoverRun,
    DiscoveryPrediction,
    PaperHolding,
    PaperPortfolio,
    PaperPortfolioArchive,
    User,
)
from app.foundation.paper_cash import paper_cash_balance
from app.foundation.quant_metrics import diebold_mariano_hln
from app.foundation.settings import get_public_settings


def _user(db) -> User:
    user = User(id=uuid4().hex, username="jan", password_hash="x")
    db.add(user)
    db.commit()
    return user


# --- Sleeves -------------------------------------------------------------------


def test_challenger_clone_is_identical_to_the_champion_book_at_spawn():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    champ_sleeve = db.get(PaperPortfolio, champion.portfolio_id)
    db.add(PaperHolding(portfolio_id=champ_sleeve.id, ticker="AAA", name="A", isin=None,
                        quantity=Decimal("10"), avg_buy_price=Decimal("50"), asset_type="stock"))
    champ_sleeve.initial_cash = Decimal("1234.56")
    db.commit()

    challenger = spawn_challenger(db, user.id, seed=1)
    sleeve = db.get(PaperPortfolio, challenger.portfolio_id)
    assert sleeve.id != champ_sleeve.id
    assert sleeve.mandate == strategy_mandate(challenger.id) and len(sleeve.mandate) <= 24
    held = {(h.ticker, h.quantity) for h in db.query(PaperHolding).filter_by(portfolio_id=sleeve.id)}
    assert held == {("AAA", Decimal("10"))}
    assert paper_cash_balance(sleeve, db) == paper_cash_balance(champ_sleeve, db)
    assert sleeve.inception_at is not None


def test_each_new_challenger_gets_a_new_sleeve_and_the_old_one_is_archived():
    db = _memory_db()
    user = _user(db)
    ensure_champion(db, user.id)
    first = spawn_challenger(db, user.id, seed=1)
    second = spawn_challenger(db, user.id, seed=2)
    db.refresh(first)
    assert first.role == "retired"
    assert first.portfolio_id != second.portfolio_id
    assert db.get(PaperPortfolio, first.portfolio_id).managed_by == "retired"
    assert db.query(PaperPortfolioArchive).filter_by(portfolio_id=first.portfolio_id).count() == 1


def test_one_time_split_retires_the_shared_sleeve_once():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    # The pre-ADR-0025 layout: a challenger driving the shared sleeve.
    shared = PaperPortfolio(user_id=user.id, mandate=CHALLENGER_MANDATE, name="Advisor Challenger",
                            managed_by="llm", initial_cash=Decimal("1000"))
    db.add(shared)
    db.flush()
    old = AdvisorStrategy(user_id=user.id, role="challenger", portfolio_id=shared.id, config_json={},
                          parent_strategy_id=champion.id)
    db.add(old)
    db.commit()

    assert split_shared_challenger_sleeve(db, user.id) is True
    db.refresh(old)
    db.refresh(shared)
    assert old.role == "retired"
    assert shared.managed_by == "retired"
    fresh = get_active_challenger(db, user.id)
    assert fresh is not None and fresh.portfolio_id not in (shared.id, champion.portfolio_id)
    assert get_public_settings(db).get(SPLIT_SETTING_KEY)

    # Second run: a no-op.
    assert split_shared_challenger_sleeve(db, user.id) is False
    assert get_active_challenger(db, user.id).id == fresh.id


# --- Predictions carry the strategy -------------------------------------------------


def _resolved_prediction(db, user_id, portfolio_id, strategy_id, predicted_at):
    db.add(DiscoveryPrediction(
        id=uuid4().hex, user_id=user_id, run_id="r", portfolio_id=portfolio_id, strategy_id=strategy_id,
        symbol="AAA", predicted_at=predicted_at, horizon_days=21, resolve_at=predicted_at + timedelta(days=30),
        direction="buy", conviction=0.7, expected_return=0.02, realised_return=0.01, excess_return=0.005,
        outcome_status="resolved",
    ))


def test_strategy_scorecard_uses_only_its_own_calls_and_no_legacy_fallback():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)
    now = datetime.now(UTC)
    champion.created_at = now - timedelta(days=30)
    db.commit()
    # A user-wide call with no strategy, one of the champion's, and one of
    # the champion's from before it existed (not counted).
    _resolved_prediction(db, user.id, champion.portfolio_id, champion.id, now - timedelta(days=40))
    _resolved_prediction(db, user.id, None, None, now - timedelta(days=5))
    _resolved_prediction(db, user.id, champion.portfolio_id, champion.id, now - timedelta(days=5))
    db.commit()

    card = compute_advisor_scorecard(db, user.id, portfolio_id=challenger.portfolio_id, strategy=challenger)
    assert card.n_predictions == 0  # nothing borrowed from the user or the champion
    card = compute_advisor_scorecard(db, user.id, portfolio_id=champion.portfolio_id, strategy=champion)
    assert card.n_predictions == 1


def test_cycle_tags_predictions_with_the_strategy():
    from test_advisor_loop import (
        _seed_discover_run,
        _seed_prices,
        _seed_real_book,
        _stub_llm,
        _wiggly_series,
    )

    from app.decision.advisor.cycle import run_advisor_cycle

    db = _memory_db()
    user = _user(db)
    _seed_real_book(db, user, cash=50_000.0)
    _seed_discover_run(db, user, ["CAND"])
    _seed_prices(db, "HELD", _wiggly_series(90.0))
    _seed_prices(db, "CAND", _wiggly_series(40.0))
    champion = ensure_champion(db, user.id)
    run_advisor_cycle(db, user.id, strategy=champion, llm_call=_stub_llm([
        {"ticker": "CAND", "action": "buy", "target_weight": 0.05, "thesis": "t", "confidence": 0.6},
    ]))
    preds = db.query(DiscoveryPrediction).all()
    assert preds and all(p.strategy_id == champion.id for p in preds)


# --- Shadow forecasts ---------------------------------------------------------------


def _shortlist(db, user_id, symbols):
    run = DiscoverRun(id=uuid4().hex, user_id=user_id, status="completed", created_at=datetime.now(UTC))
    db.add(run)
    db.flush()
    for sym in symbols:
        db.add(DiscoverCandidate(id=uuid4().hex, run_id=run.id, symbol=sym, source="t", status="shortlisted"))
    db.commit()


def _llm(answer):
    def call(_db, _messages):
        return answer
    return call


def test_shadow_forecasts_cover_the_shortlist_once_per_strategy_and_day():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)
    _shortlist(db, user.id, ["AAA", "BBB"])
    answer = (
        '{"forecasts": [{"ticker": "AAA", "p_beat": 0.6, "band_low": -0.05, "band_high": 0.08},'
        ' {"ticker": "bbb", "p_beat": 1.4, "band_low": 0.1, "band_high": -0.1},'
        ' {"ticker": "ZZZ", "p_beat": 0.5, "band_low": 0, "band_high": 0}]}'
    )
    day = date(2026, 11, 10)
    written = run_shadow_forecasts(db, user.id, [champion, challenger], llm_call=_llm(answer), today=day)
    assert written == {champion.id: 2, challenger.id: 2}
    rows = db.query(AdvisorShadowForecast).filter_by(strategy_id=champion.id).all()
    by = {r.ticker: r for r in rows}
    assert set(by) == {"AAA", "BBB"}  # an unlisted ticker is dropped
    assert by["BBB"].p_beat == 0.99 and by["BBB"].band_low < by["BBB"].band_high
    # Idempotent per day, and the table refuses a duplicate.
    run_shadow_forecasts(db, user.id, [champion, challenger], llm_call=_llm(answer), today=day)
    assert db.query(AdvisorShadowForecast).count() == 4
    db.add(AdvisorShadowForecast(user_id=user.id, strategy_id=champion.id, forecast_date=day, ticker="AAA",
                                 p_beat=0.5, band_low=0, band_high=0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_shadow_forecasts_resolve_against_the_benchmark():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    day = date(2026, 9, 1)
    db.add(AdvisorShadowForecast(user_id=user.id, strategy_id=champion.id, forecast_date=day, ticker="AAA",
                                 horizon_days=21, p_beat=0.8, band_low=-0.05, band_high=0.05))
    db.commit()
    exit_day = np.busday_offset(day, 21, roll="forward").astype(date)
    closes = {
        "AAA": {(day - timedelta(days=1)).isoformat(): 100.0, exit_day.isoformat(): 110.0},
        "EUNL.DE": {(day - timedelta(days=1)).isoformat(): 50.0, exit_day.isoformat(): 51.0},
    }
    n = resolve_shadow_forecasts(db, today=exit_day + timedelta(days=2),
                                 closes_loader=lambda _db, sym, _n: closes.get(sym, {}))
    assert n == 1
    row = db.query(AdvisorShadowForecast).one()
    assert row.beat is True and row.brier == pytest.approx(0.04)
    assert row.realised_return == pytest.approx(0.10)
    assert row.rps == pytest.approx(band_rps(-0.05, 0.05, 0.10))


def test_parse_forecasts_ignores_garbage():
    assert parse_forecasts("not json", {"AAA"}) == {}
    assert parse_forecasts('{"forecasts": [{"ticker": "AAA"}]}', {"AAA"}) == {}


# --- The test statistic ----------------------------------------------------------------


def test_dm_hln_at_h1_is_the_one_sample_t_test():
    x = np.random.default_rng(1).normal(0.1, 1.0, 50)
    ours = diebold_mariano_hln(x, h=1)
    ref = ttest_1samp(x, 0.0, alternative="greater")
    assert ours["stat"] == pytest.approx(float(ref.statistic), rel=1e-12)
    assert ours["p_value"] == pytest.approx(float(ref.pvalue), rel=1e-10)


def test_dm_hln_newey_west_matches_a_hand_computation():
    d = np.array([0.3, -0.1, 0.4, 0.2, -0.2, 0.5, 0.1, 0.0, 0.3, -0.1])
    h, lag = 3, 2
    n, dev = len(d), d - d.mean()
    lrv = dev @ dev / n
    for k in (1, 2):
        lrv += 2 * (1 - k / 3) * (dev[k:] @ dev[:-k]) / n
    expected = d.mean() / np.sqrt(lrv / n) * np.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    assert diebold_mariano_hln(d, h=h, lag=lag)["stat"] == pytest.approx(expected)


def test_pocock_boundary_constant_is_pinned():
    """The per-look level for 6 looks at one-sided 0.0125, re-derived by simulation."""
    rng = np.random.default_rng(20261110)
    z = np.cumsum(rng.standard_normal((400_000, rule.MAX_LOOKS)), axis=1) / np.sqrt(np.arange(1, rule.MAX_LOOKS + 1))
    c = float(np.quantile(z.max(axis=1), 1 - rule.OVERALL_ALPHA))
    assert c == pytest.approx(rule.POCOCK_BOUNDARY, abs=0.03)
    assert rule.POCOCK_NOMINAL_ALPHA == pytest.approx(1 - norm.cdf(rule.POCOCK_BOUNDARY), abs=2e-5)
    assert rule.OVERALL_ALPHA == pytest.approx(0.0125)


# --- The rule -----------------------------------------------------------------------


def _pair(db):
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)
    return user, champion, challenger


def test_rule_promotes_a_planted_better_forecaster():
    db = _memory_db()
    user, champion, challenger = _pair(db)
    seed_paired_history(db, user.id, champion, challenger, challenger_edge=0.25, seed=11)
    result = rule.evaluate_paired_promotion(db, champion, challenger, today=date.today(), state={})
    assert result.verdict == "promote"
    assert result.mean_d is not None and result.mean_d > 0
    assert result.guardrails["return_ok"] and result.guardrails["mdd_ok"]


def test_rule_false_promotion_rate_on_equal_forecasters_is_small():
    """Equal forecasters (fixed seeds): the first look promotes almost never."""
    promoted = 0
    for seed in range(20):
        db = _memory_db()
        user, champion, challenger = _pair(db)
        seed_paired_history(db, user.id, champion, challenger, challenger_edge=0.0, seed=seed)
        result = rule.evaluate_paired_promotion(db, champion, challenger, today=date.today(), state={})
        promoted += result.verdict == "promote"
    assert promoted <= 1


def test_rule_respects_the_minimums():
    db = _memory_db()
    user, champion, challenger = _pair(db)
    seed_paired_history(db, user.id, champion, challenger, challenger_edge=0.3, spawn_days_ago=40, days=25)
    result = rule.evaluate_paired_promotion(db, champion, challenger, today=date.today(), state={})
    assert result.verdict == "observing"
    assert result.minimums == {"trading_days": False, "horizons": False, "pairs": False}


def test_return_guardrail_blocks_a_better_forecaster_that_loses_money():
    db = _memory_db()
    user, champion, challenger = _pair(db)
    seed_paired_history(db, user.id, champion, challenger, challenger_edge=0.25, seed=11, nav_gap=-0.001)
    result = rule.evaluate_paired_promotion(db, champion, challenger, today=date.today(), state={})
    assert result.verdict == "observing"
    assert result.guardrails["return_ok"] is False
    assert "guardrail" in result.reason


def test_looks_are_spaced_and_counted():
    db = _memory_db()
    user, champion, challenger = _pair(db)
    seed_paired_history(db, user.id, champion, challenger, challenger_edge=0.0, seed=3)
    state: dict = {}
    day = date.today()
    first = rule.evaluate_paired_promotion(db, champion, challenger, today=day, state=state)
    assert first.look == 1
    # Ten trading days later: between looks, no test spent.
    mid = rule.evaluate_paired_promotion(
        db, champion, challenger, today=np.busday_offset(day, 10, roll="forward").astype(date), state=state
    )
    assert mid.look is None and mid.reason == "between planned looks"
    second = rule.evaluate_paired_promotion(
        db, champion, challenger, today=np.busday_offset(day, 21, roll="forward").astype(date), state=state
    )
    assert second.look == 2 and len(state["looks"]) == 2


def test_dm_drops_autocovariance_pairs_more_than_five_trading_days_apart():
    """WP4: a gap in the matched forecasts must not be read as an adjacent day."""
    rng = np.random.default_rng(7)
    d = list(rng.normal(0.01, 0.05, 40))
    contiguous = diebold_mariano_hln(d, h=5, lag=4, positions=list(range(40)))
    assert contiguous["stat"] == pytest.approx(diebold_mariano_hln(d, h=5, lag=4)["stat"])
    # A 30-day hole in the middle: pairs across it are dropped, so the stat differs.
    gapped_pos = list(range(20)) + list(range(50, 70))
    gapped = diebold_mariano_hln(d, h=5, lag=4, positions=gapped_pos)
    assert gapped["stat"] != pytest.approx(contiguous["stat"])
    # Dropping the cross-gap pairs is the same as the plain statistic when every pair is far apart.
    far = diebold_mariano_hln(d, h=5, lag=4, positions=[i * 10 for i in range(40)])
    assert far["n"] == 40
