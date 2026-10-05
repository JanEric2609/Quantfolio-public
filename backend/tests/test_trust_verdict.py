"""Tests for decision/verification/trust.py and /api/trust/* ("Can I trust it?").

Real ledger rows in an in-memory database; nothing is mocked. The statistics
themselves are covered in test_forecast_verification.py, so these tests pin the
ledger readers, the per-type definitions, the evidence states and the wire shape.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification.trust import build_verdict, list_calls
from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import (
    DiscoveryPrediction,
    LlmPortfolioDecision,
    PaperPortfolio,
    RegimeLabelHistory,
    User,
)
from app.main import app

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
START = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db, name: str = "trust") -> User:
    user = User(username=name, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _sleeve(db, user: User, mandate: str = "advisor") -> PaperPortfolio:
    sleeve = PaperPortfolio(
        user_id=user.id, name=f"{mandate} sleeve", currency="EUR", initial_cash=10000,
        baseline_value=10000, mandate=mandate, managed_by="llm",
    )
    db.add(sleeve)
    db.commit()
    db.refresh(sleeve)
    return sleeve


def _pred(
    db,
    user: User,
    i: int,
    *,
    hit: bool | None = True,
    sleeve: PaperPortfolio | None = None,
    p: float | None = None,
    direction: str = "buy",
    low: float | None = None,
    high: float | None = None,
    realised: float | None = None,
    status: str = "resolved",
    symbol: str | None = None,
    excess: float | None = None,
    resolve_in_days: int = -1,
) -> DiscoveryPrediction:
    issued = START + timedelta(days=i)
    resolved_on = issued + timedelta(days=30)
    bench = 0.01
    if realised is None:
        realised = 0.03 if hit else -0.04
    if excess is None and hit is not None:
        excess = 0.02 if hit else -0.05
    row = DiscoveryPrediction(
        id=str(uuid4()),
        user_id=user.id,
        run_id="run",
        symbol=symbol or f"SYM{i % 7}",
        predicted_at=issued,
        horizon_days=21,
        resolve_at=NOW + timedelta(days=resolve_in_days) if status == "pending" else resolved_on,
        direction=direction,
        conviction=0.6,
        conviction_calibrated=p,
        expected_return_low=low,
        expected_return_high=high,
        realised_return=realised if status == "resolved" else None,
        benchmark_return=bench if status == "resolved" else None,
        excess_return=excess if status == "resolved" else None,
        outcome_status=status,
        score_json={"outcome": {"exit_date": resolved_on.date().isoformat()}} if status == "resolved" else None,
        portfolio_id=sleeve.id if sleeve else None,
        features_json={},
    )
    db.add(row)
    return row


def _seed_hit_rate(db, user, n: int, hit_pct: int, **kw) -> None:
    """*n* resolved calls with ``hit_pct`` % hits spread evenly through time."""
    for i in range(n):
        hit = ((i + 1) * hit_pct) // 100 - (i * hit_pct) // 100 == 1
        _pred(db, user, i, hit=hit, **kw)
    db.commit()


def _type(verdict: dict, key: str) -> dict:
    return next(t for t in verdict["types"] if t["type"] == key)


# ---------------------------------------------------------------------------
# Verdict: states
# ---------------------------------------------------------------------------


def test_empty_ledgers_are_too_early_everywhere_with_a_plain_headline():
    db = _memory_db()
    user = _user(db)

    verdict = build_verdict(db, user.id, now=NOW)

    assert verdict["state"] == "too_early"
    assert verdict["headline"] == "Too early to tell: nothing has resolved yet."
    assert verdict["resolved_calls"] == 0
    assert verdict["skill"] is None
    assert verdict["frozen_at_issue"] is True
    assert [t["type"] for t in verdict["types"]] == ["ideas", "advisor", "mandates", "regime"]
    for t in verdict["types"]:
        assert t["state"] == "too_early"
        assert t["n"] == 0 and t["hits"] == 0
        assert t["hit_rate"] is None and t["hit_ci"] is None
        assert t["mean_excess"] is None and t["mean_excess_ci"] is None
    assert _type(verdict, "ideas")["n_needed"] == 617  # 55 % vs a coin flip
    assert verdict["n_needed"] == 617 and verdict["resolved_units"] == 0
    assert _type(verdict, "regime")["n_needed"] is None
    assert "No daily regime calls recorded yet" in _type(verdict, "regime")["note"]
    assert "617" in verdict["method_note"]


def test_headline_names_the_first_due_resolution_when_only_pending_calls_exist():
    db = _memory_db()
    user = _user(db)
    for i, days in enumerate([14, 6, 20]):
        _pred(db, user, i, hit=None, status="pending", resolve_in_days=days)
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)

    due = (NOW + timedelta(days=6)).date().isoformat()
    assert verdict["headline"] == f"Too early to tell: nothing has resolved yet. First resolutions due {due}."
    assert _type(verdict, "ideas")["next_resolution_at"].startswith(due)
    assert _type(verdict, "advisor")["next_resolution_at"] is None


def test_a_dozen_calls_is_too_early_with_a_wide_interval():
    db = _memory_db()
    user = _user(db)
    for i in range(12):
        _pred(db, user, i, hit=i % 4 != 3)  # 9 of 12, interleaved with the misses
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)
    ideas = _type(verdict, "ideas")

    assert verdict["state"] == "too_early"
    assert verdict["headline"] == (
        "Too early to tell: 12 of ~600 independent rebalance dates needed to tell a 55 % hit rate from a coin flip."
    )
    assert (ideas["n"], ideas["hits"], ideas["hit_rate"]) == (12, 9, 0.75)
    lo, hi = ideas["hit_ci"]
    assert lo < 0.75 < hi and (hi - lo) > 0.3
    lo, hi = ideas["mean_excess_ci"]  # Newey-West from 8 dates on
    assert lo < ideas["mean_excess"] < hi
    assert ideas["state"] == "too_early"


def test_two_hundred_calls_at_seventy_percent_show_evidence_of_skill():
    db = _memory_db()
    user = _user(db)
    _seed_hit_rate(db, user, 200, 70)

    verdict = build_verdict(db, user.id, now=NOW)
    ideas = _type(verdict, "ideas")

    assert verdict["state"] == "skill"
    assert verdict["headline"] == "Evidence of skill vs your ETF over 200 rebalance dates (after correcting for every look)."
    assert ideas["state"] == "skill"
    assert (ideas["n"], ideas["hits"]) == (200, 140)
    assert ideas["hit_rate"] == pytest.approx(0.7)
    lo, hi = ideas["hit_ci"]
    assert lo < 0.7 < hi and lo > 0.5
    assert ideas["e_skill"] >= 20 and ideas["e_skill_crossed_strong"] is True
    assert ideas["e_harm"] < 20
    assert ideas["n_issue_days"] == 200
    assert verdict["resolved_calls"] == 200
    skill = verdict["skill"]
    assert skill["metric"] == "mean_excess_return_per_date" and skill["n"] == 200
    assert skill["value"] == pytest.approx(0.7 * 0.02 + 0.3 * -0.05)
    assert skill["ci_low"] is not None and skill["ci_low"] <= skill["value"] <= skill["ci_high"]
    assert "EUNL.DE" in skill["benchmark_label"]
    # Other types stay untouched.
    assert _type(verdict, "advisor")["state"] == "too_early"


def test_two_hundred_calls_at_thirty_percent_show_evidence_of_harm():
    db = _memory_db()
    user = _user(db)
    _seed_hit_rate(db, user, 200, 30)

    verdict = build_verdict(db, user.id, now=NOW)

    assert verdict["state"] == "harm"
    assert _type(verdict, "ideas")["state"] == "harm"
    assert _type(verdict, "ideas")["e_harm"] >= 20
    assert verdict["headline"].startswith("Evidence of harm")
    assert verdict["skill"]["value"] < 0


def test_a_coin_flip_record_is_no_evidence_not_skill_or_harm():
    db = _memory_db()
    user = _user(db)
    for i in range(60):
        _pred(db, user, i, hit=i % 2 == 0)
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)

    assert verdict["state"] == "no_evidence"
    assert _type(verdict, "ideas")["state"] == "no_evidence"
    assert verdict["headline"].startswith("No evidence yet that the calls beat your ETF")


def test_harm_in_one_type_outranks_skill_in_another():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user)
    _seed_hit_rate(db, user, 200, 70)
    _seed_hit_rate(db, user, 200, 30, sleeve=sleeve)

    verdict = build_verdict(db, user.id, now=NOW)

    assert _type(verdict, "ideas")["state"] == "skill"
    assert _type(verdict, "advisor")["state"] == "harm"
    assert verdict["state"] == "harm"


# ---------------------------------------------------------------------------
# Verdict: scoping and definitions
# ---------------------------------------------------------------------------


def test_other_users_calls_do_not_count():
    db = _memory_db()
    mine = _user(db, "mine")
    other = _user(db, "other")
    _seed_hit_rate(db, other, 200, 70)
    sleeve = _sleeve(db, other)
    _seed_hit_rate(db, other, 50, 90, sleeve=sleeve)

    verdict = build_verdict(db, mine.id, now=NOW)
    assert verdict["resolved_calls"] == 0
    assert verdict["state"] == "too_early"
    assert list_calls(db, mine.id, now=NOW)["total"] == 0

    assert build_verdict(db, other.id, now=NOW)["resolved_calls"] == 250


def test_ideas_and_advisor_are_separated_by_the_paper_sleeve():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user)
    for i in range(10):
        _pred(db, user, i, hit=True)
    for i in range(6):
        _pred(db, user, 100 + i, hit=False, sleeve=sleeve)
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)

    assert (_type(verdict, "ideas")["n"], _type(verdict, "ideas")["hits"]) == (10, 10)
    assert (_type(verdict, "advisor")["n"], _type(verdict, "advisor")["hits"]) == (6, 0)


def test_logged_holds_pending_and_unbenchmarked_rows_are_not_hit_statistics_but_delisted_ones_are_misses():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user)
    _pred(db, user, 0, hit=True, sleeve=sleeve)  # the one directional, benchmarked call
    _pred(db, user, 1, hit=True, sleeve=sleeve, direction="neutral")  # a logged hold
    _pred(db, user, 2, hit=None, sleeve=sleeve, status="pending", resolve_in_days=5)
    _pred(db, user, 3, hit=None, sleeve=sleeve, status="delisted")
    # Resolved before the benchmark was recorded: raw return only.
    row = _pred(db, user, 4, hit=True, sleeve=sleeve)
    row.excess_return = None
    db.commit()

    advisor = _type(build_verdict(db, user.id, now=NOW), "advisor")

    # The delisted pick with no price at all is a miss on its own date, not a gap.
    assert advisor["n"] == 2 and advisor["hits"] == 1
    assert advisor["n_calls"] == 2 and advisor["n_delisted"] == 1
    assert advisor["next_resolution_at"] is not None


def test_picks_of_one_rebalance_date_are_one_unit():
    db = _memory_db()
    user = _user(db)
    # Ten dates, seventeen picks each: 170 picks but ten independent observations.
    for day in range(10):
        for k in range(17):
            row = _pred(db, user, day, hit=k < 9, symbol=f"S{k}")
            row.excess_return = 0.02 if k < 9 else -0.01
    db.commit()

    ideas = _type(build_verdict(db, user.id, now=NOW), "ideas")

    assert (ideas["n"], ideas["n_calls"]) == (10, 170)
    assert ideas["hits"] == 10  # every basket beat the ETF
    assert ideas["mean_excess"] == pytest.approx((9 * 0.02 - 8 * 0.01) / 17)
    assert ideas["hac_lag"] > 0  # 21-day windows of daily dates overlap
    assert ideas["state"] == "too_early"  # 10 dates, however many picks


def test_a_pick_that_stopped_trading_counts_at_its_last_close():
    db = _memory_db()
    user = _user(db)
    row = _pred(db, user, 0, hit=False)
    row.outcome_status = "delisted"
    row.score_json = {"outcome": {"exit_date": "2026-03-10", "exit_basis": "last_close", "benchmark": "EUNL.DE"}}
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)
    ideas = _type(verdict, "ideas")
    item = list_calls(db, user.id, now=NOW)["items"][0]

    assert (ideas["n"], ideas["hits"], ideas["n_delisted"]) == (1, 0, 1)
    assert item["delisted"] is True and "stopped trading" in item["call"]
    assert ideas["benchmark_label"] == "your passive core ETF (EUNL.DE)"


def test_one_crossing_among_several_looks_is_not_yet_evidence():
    """e-BH: with four tests, the strongest needs 4 / 0.05 = 80, not 20."""
    from app.decision.verification.trust import _apply_e_bh

    base = {"n": 40, "n_needed": 617, "benchmarked": True}
    stats = [{**base, "e_skill": 30.0, "e_harm": 0.5}, {**base, "e_skill": 1.0, "e_harm": 1.0}]
    assert _apply_e_bh(stats) == 4
    assert stats[0]["state"] == "no_evidence"
    stats[0]["e_skill"] = 90.0
    _apply_e_bh(stats)
    assert stats[0]["state"] == "skill" and stats[1]["state"] == "no_evidence"


def test_a_sell_call_is_a_hit_when_it_beat_the_benchmark_in_its_own_direction():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user)
    # The resolver already signed the excess for a sell; the ledger stores it as is.
    _pred(db, user, 0, hit=True, sleeve=sleeve, direction="sell", realised=0.05, excess=0.06)
    _pred(db, user, 1, hit=False, sleeve=sleeve, direction="sell", realised=-0.02, excess=-0.03)
    db.commit()

    calls = list_calls(db, user.id, type="advisor", now=NOW)["items"]

    assert sorted(c["hit"] for c in calls) == [False, True]
    assert {c["call"] for c in calls} == {"SELL · 21 days"}


def test_brier_skill_calibration_caption_and_reliability_from_stated_probabilities():
    db = _memory_db()
    user = _user(db)
    # 50 calls stated at 80 % that hit 40 times; 50 stated at 30 % that hit 15 times.
    for i in range(50):
        _pred(db, user, i, hit=i < 40, p=0.8)
    for i in range(50):
        _pred(db, user, 100 + i, hit=i < 15, p=0.3)
    db.commit()

    ideas = _type(build_verdict(db, user.id, now=NOW), "ideas")

    assert ideas["n_stated_p"] == 100
    expected_brier = (40 * 0.2**2 + 10 * 0.8**2 + 15 * 0.7**2 + 35 * 0.3**2) / 100
    assert ideas["brier"] == pytest.approx(expected_brier)
    assert ideas["bss"] > 0
    lo, hi = ideas["bss_ci"]
    assert lo <= ideas["bss"] <= hi
    assert ideas["calibration_caption"] == "When we said 80 %, it happened 40 of 50 times."
    assert len(ideas["reliability"]) == 5 and sum(b["n"] for b in ideas["reliability"]) == 100
    assert ideas["spiegelhalter_z"] is not None


def test_reliability_is_withheld_below_thirty_stated_calls_and_caption_needs_five_per_bucket():
    db = _memory_db()
    user = _user(db)
    for i in range(12):
        _pred(db, user, i, hit=i < 6, p=0.7)
    for i in range(2):
        _pred(db, user, 50 + i, hit=True, p=0.2)
    db.commit()

    ideas = _type(build_verdict(db, user.id, now=NOW), "ideas")

    assert ideas["n_stated_p"] == 14
    assert ideas["reliability"] == []
    assert ideas["calibration_caption"] == "When we said 70 %, it happened 6 of 12 times."
    assert ideas["bss_ci"] is None  # the Brier-skill interval needs at least 15 stated calls

    _pred(db, user, 60, hit=True, p=0.2)
    db.commit()
    assert _type(build_verdict(db, user.id, now=NOW), "ideas")["bss_ci"] is not None


def test_no_stated_probabilities_means_no_brier_and_no_caption():
    db = _memory_db()
    user = _user(db)
    _seed_hit_rate(db, user, 30, 60)

    ideas = _type(build_verdict(db, user.id, now=NOW), "ideas")

    assert ideas["n_stated_p"] == 0
    assert ideas["brier"] is None and ideas["bss"] is None and ideas["calibration_caption"] is None


def test_range_coverage_counts_stock_returns_inside_the_stated_range():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user)
    # Discover ideas (nominal 80 %): 16 of 20 inside [-5 %, +5 %].
    for i in range(20):
        _pred(db, user, i, hit=True, low=-0.05, high=0.05, realised=0.02 if i < 16 else 0.09)
    # Advisor (nominal 90 %): a sell's realised return is direction-signed, the
    # range is on the stock: realised +0.04 for a sell means the stock fell 4 %.
    _pred(db, user, 100, hit=True, sleeve=sleeve, direction="sell", low=-0.06, high=0.06, realised=0.04)
    _pred(db, user, 101, hit=True, sleeve=sleeve, direction="sell", low=-0.06, high=0.06, realised=0.09)
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)
    ideas_cov = _type(verdict, "ideas")["range_coverage"]
    advisor_cov = _type(verdict, "advisor")["range_coverage"]

    assert (ideas_cov["k"], ideas_cov["n"], ideas_cov["nominal"]) == (16, 20, 0.8)
    assert ideas_cov["rate"] == 0.8 and ideas_cov["ci_low"] < 0.8 < ideas_cov["ci_high"]
    assert (advisor_cov["k"], advisor_cov["n"], advisor_cov["nominal"]) == (1, 2, 0.9)


def test_rows_without_ranges_have_no_coverage():
    db = _memory_db()
    user = _user(db)
    _seed_hit_rate(db, user, 10, 50)
    assert _type(build_verdict(db, user.id, now=NOW), "ideas")["range_coverage"] is None


# ---------------------------------------------------------------------------
# Mandates and regime
# ---------------------------------------------------------------------------


def _decision(db, sleeve: PaperPortfolio, i: int, verdict: str | None, *, confidence=None, status="completed") -> None:
    review = START + timedelta(weeks=i)
    payload = {
        "ticker": f"MND{i}",
        "expectation": {
            "metric": "benchmark_excess_pct",
            "direction": "increase",
            "magnitude": 2.0,
            "horizon_weeks": 4,
            "confidence": confidence,
        },
    }
    reflection = {"actual_outcome": {"actual_pct": 3.5, "verdict": verdict}} if verdict else None
    db.add(LlmPortfolioDecision(
        portfolio_id=sleeve.id, review_date=review, mandate=sleeve.mandate,
        decision_json=json.dumps(payload), reflection_json=json.dumps(reflection) if reflection else None,
        status=status, verdict=verdict, horizon_weeks=4, scored_at=review + timedelta(weeks=4) if verdict else None,
    ))


def test_mandate_hit_rate_excludes_unresolvable_and_counts_partial_as_not_hit():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user, "A")
    verdicts = ["hit"] * 5 + ["partial"] * 2 + ["miss"] * 3 + ["unresolvable"] * 4
    for i, v in enumerate(verdicts):
        _decision(db, sleeve, i, v, confidence=0.7 if i < 6 else None)
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)
    mandates = _type(verdict, "mandates")

    assert (mandates["n"], mandates["hits"], mandates["hit_rate"]) == (10, 5, 0.5)
    assert mandates["benchmarked"] is False
    assert "coin flip" in mandates["benchmark_label"]
    assert mandates["n_stated_p"] == 6
    # Mandates have no naive benchmark, so they never drive the overall state.
    assert verdict["state"] == "too_early" and verdict["resolved_calls"] == 10


def test_pending_mandate_decisions_set_the_next_resolution_and_other_users_are_excluded():
    db = _memory_db()
    mine = _user(db, "mine")
    other = _user(db, "other")
    my_sleeve = _sleeve(db, mine, "A")
    other_sleeve = _sleeve(db, other, "B")
    # A decision reviewed 2 weeks before NOW with a 4-week horizon resolves in 2 weeks.
    db.add(LlmPortfolioDecision(
        portfolio_id=my_sleeve.id, review_date=NOW - timedelta(weeks=2), mandate="A",
        decision_json="{}", status="completed", verdict=None, horizon_weeks=4,
    ))
    for i in range(8):
        _decision(db, other_sleeve, i, "hit")
    db.commit()

    mandates = _type(build_verdict(db, mine.id, now=NOW), "mandates")

    assert mandates["n"] == 0
    assert mandates["next_resolution_at"].startswith((NOW + timedelta(weeks=2)).date().isoformat())
    assert _type(build_verdict(db, other.id, now=NOW), "mandates")["n"] == 8


def test_mandate_calls_table_row_carries_the_expectation_and_the_verdict():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user, "A")
    _decision(db, sleeve, 0, "miss", confidence=0.65)
    db.commit()

    (item,) = list_calls(db, user.id, type="mandates", now=NOW)["items"]

    assert item["type"] == "mandates" and item["subject"] == "MND0"
    assert item["call"] == "benchmark excess: increase by 2 % in 4 weeks"
    assert item["verdict"] == "miss" and item["hit"] is False
    assert item["stated_p"] == 0.65 and item["outcome"] == pytest.approx(0.035)
    assert item["excess"] is None and item["benchmark_outcome"] is None


def test_regime_history_accumulates_but_is_not_scored():
    db = _memory_db()
    user = _user(db)
    for d in range(1, 6):
        db.add(RegimeLabelHistory(as_of=date(2026, 9, d), label="bull", probabilities_json={"bull": 0.7}, model="jump"))
    db.commit()

    regime = _type(build_verdict(db, user.id, now=NOW), "regime")

    assert regime["state"] == "too_early" and regime["n"] == 0
    assert regime["benchmarked"] is False
    assert "5 daily regime calls recorded since 2026-09-01" in regime["note"]
    assert "no agreed realised-state definition" in regime["note"]


# ---------------------------------------------------------------------------
# Calls table
# ---------------------------------------------------------------------------


def test_calls_are_newest_first_filtered_paged_and_frozen():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user)
    for i in range(8):
        _pred(db, user, i, hit=i % 2 == 0, low=-0.05 if i == 0 else None, high=0.05 if i == 0 else None)
    for i in range(3):
        _pred(db, user, 50 + i, hit=True, sleeve=sleeve)
    db.commit()

    everything = list_calls(db, user.id, now=NOW)
    assert everything["total"] == 11 and everything["frozen_at_issue"] is True
    resolved = [c["resolved_at"] for c in everything["items"]]
    assert resolved == sorted(resolved, reverse=True)

    ideas = list_calls(db, user.id, type="ideas", limit=3, offset=2, now=NOW)
    assert ideas["type"] == "ideas" and ideas["total"] == 8
    assert len(ideas["items"]) == 3 and {c["type"] for c in ideas["items"]} == {"ideas"}

    oldest = list_calls(db, user.id, type="ideas", limit=50, now=NOW)["items"][-1]
    assert oldest["stated_range"] == [-0.05, 0.05]
    assert oldest["outcome"] == 0.03 and oldest["benchmark_outcome"] == 0.01 and oldest["excess"] == 0.02
    assert oldest["hit"] is True and oldest["subject"].startswith("SYM")
    assert oldest["issued_at"] < oldest["resolved_at"]


def test_worst_misses_are_the_five_most_negative_excess_returns():
    db = _memory_db()
    user = _user(db)
    losses = [-0.01, -0.09, -0.04, -0.12, -0.02, -0.07, -0.03]
    for i, loss in enumerate(losses):
        _pred(db, user, i, hit=False, excess=loss, realised=loss + 0.01)
    _pred(db, user, 20, hit=True)
    db.commit()

    misses = list_calls(db, user.id, now=NOW)["worst_misses"]

    assert [m["excess"] for m in misses] == [-0.12, -0.09, -0.07, -0.04, -0.03]
    assert all(m["hit"] is False for m in misses)


def test_no_worst_misses_when_every_call_beat_the_benchmark():
    db = _memory_db()
    user = _user(db)
    _seed_hit_rate(db, user, 5, 100)
    assert list_calls(db, user.id, now=NOW)["worst_misses"] == []


def test_unknown_type_filter_falls_back_to_every_type():
    db = _memory_db()
    user = _user(db)
    _seed_hit_rate(db, user, 3, 100)
    result = list_calls(db, user.id, type="nonsense", now=NOW)
    assert result["type"] is None and result["total"] == 3


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    db = _memory_db()
    user = _user(db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        yield TestClient(app), db, user
    finally:
        app.dependency_overrides.clear()


def test_verdict_endpoint_empty_ledger_shape(client):
    http, _db, _user_row = client

    body = http.get("/api/trust/verdict").json()

    assert body["state"] == "too_early" and body["resolved_calls"] == 0
    assert body["headline"].startswith("Too early to tell")
    assert body["frozen_at_issue"] is True and body["method_note"]
    assert [t["type"] for t in body["types"]] == ["ideas", "advisor", "mandates", "regime"]
    ideas = body["types"][0]
    for key in (
        "label", "n", "n_needed", "hits", "hit_rate", "hit_ci", "mean_excess", "mean_excess_ci",
        "e_skill", "e_harm", "state", "brier", "bss", "bss_ci", "calibration_caption", "next_resolution_at",
    ):
        assert key in ideas, key


def test_verdict_endpoint_reports_skill_for_a_seeded_record(client):
    http, db, user = client
    _seed_hit_rate(db, user, 200, 70, p=0.7)

    body = http.get("/api/trust/verdict").json()

    assert body["state"] == "skill"
    ideas = body["types"][0]
    assert ideas["n"] == 200 and ideas["hit_ci"][0] < 0.7 < ideas["hit_ci"][1]
    assert ideas["calibration_caption"] == "When we said 70 %, it happened 140 of 200 times."
    assert body["skill"]["n"] == 200


def test_calls_endpoint_validates_and_serves_pages(client):
    http, db, user = client
    _seed_hit_rate(db, user, 12, 50)

    page = http.get("/api/trust/calls", params={"type": "ideas", "limit": 5, "offset": 5}).json()

    assert page["total"] == 12 and len(page["items"]) == 5 and page["limit"] == 5 and page["offset"] == 5
    assert page["frozen_at_issue"] is True and len(page["worst_misses"]) == 5
    assert {"type", "id", "issued_at", "resolved_at", "subject", "call", "stated_p", "stated_range",
            "outcome", "benchmark_outcome", "excess", "hit"} <= set(page["items"][0])

    assert http.get("/api/trust/calls", params={"type": "bogus"}).status_code == 422
    assert http.get("/api/trust/calls", params={"limit": 0}).status_code == 422
    assert http.get("/api/trust/calls", params={"limit": 500}).status_code == 422
    assert http.get("/api/trust/calls", params={"offset": -1}).status_code == 422


def test_trust_endpoints_require_a_signed_in_user():
    app.dependency_overrides.clear()
    http = TestClient(app)
    assert http.get("/api/trust/verdict").status_code in (401, 403)
    assert http.get("/api/trust/calls").status_code in (401, 403)
