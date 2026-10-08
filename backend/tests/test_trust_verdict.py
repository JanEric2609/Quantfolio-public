"""Tests for decision/verification/trust.py and /api/trust/* ("Can I trust it?").

Real ledger rows in an in-memory database; nothing is mocked. The statistics
themselves are covered in test_forecast_verification.py, so these tests pin the
ledger readers, the per-type definitions, the evidence states and the wire shape.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification.daily_tests import PRIMARY_TEST_START
from app.decision.verification.trust import build_verdict, list_calls
from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import (
    DiscoveryPrediction,
    LlmPortfolioDecision,
    PaperPortfolio,
    RegimeLabelHistory,
    TrustDailyActiveReturn,
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
    issued = START + timedelta(days=i + 2 * (i // 5))  # one issue date per trading day (START is a Monday)
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


def _seed_excess(db, user, n: int, hit_pct: int, win: float, loss: float, **kw) -> None:
    """Like ``_seed_hit_rate`` but with explicit excess returns: the skill/harm tests bet on their size."""
    for i in range(n):
        hit = ((i + 1) * hit_pct) // 100 - (i * hit_pct) // 100 == 1
        _pred(db, user, i, hit=hit, excess=win if hit else loss, **kw)
    db.commit()


# +12 % on a winning date and -2 % on a losing one (clipped at +-20 %): a record the excess-return test can see.
SKILL = dict(hit_pct=90, win=0.12, loss=-0.02)
HARM = dict(hit_pct=10, win=0.02, loss=-0.12)


def _type(verdict: dict, key: str) -> dict:
    return next(t for t in verdict["types"] if t["type"] == key)


# ---------------------------------------------------------------------------
# Verdict: states (from the pre-registered calendar-time tests, ADR 0018)
# ---------------------------------------------------------------------------


def _seed_daily(db, user, series: str, values: list[float | None], *, first: date = PRIMARY_TEST_START) -> None:
    """Frozen daily-ledger rows on consecutive business days from *first*."""
    days = pd.bdate_range(first, periods=len(values))
    for day, value in zip(days, values, strict=True):
        db.add(TrustDailyActiveReturn(
            user_id=user.id, series=series, day=day.date(), n_open=0 if value is None else 15, n_stale=0,
            value=value, benchmark="EUNL.DE", detail_json={},
        ))
    db.commit()


def _daily(values_mean: float, n: int, sd: float = 0.004, seed: int = 7) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(rng.normal(values_mean, sd, n))


def _family(verdict: dict, key: str) -> dict:
    return next(f for f in verdict["daily_tests"]["families"] if f["family"] == key)


def test_empty_ledgers_are_too_early_everywhere_with_a_plain_headline():
    db = _memory_db()
    user = _user(db)

    verdict = build_verdict(db, user.id, now=NOW)

    assert verdict["state"] == "too_early"
    assert verdict["headline"] == (
        "Too early to tell: the pre-registered test starts on 2026-10-12; no trading day with open picks recorded yet."
    )
    assert verdict["resolved_calls"] == 0
    assert verdict["skill"] is None
    assert verdict["frozen_at_issue"] is True
    assert [t["type"] for t in verdict["types"]] == ["ideas", "advisor", "mandates", "regime"]
    for t in verdict["types"]:
        assert t["state"] == "too_early"
        assert t["n"] == 0 and t["hits"] == 0
        assert t["hit_rate"] is None and t["hit_ci"] is None
        assert t["mean_excess"] is None and t["mean_excess_ci"] is None
    # Legacy (secondary) per-date test: simulated median issue days to e >= 40 (K=2, h=21).
    assert _type(verdict, "ideas")["n_needed"] == 3503  # +1 % per date edge, 5 % noise
    assert verdict["n_needed"] == 3503 and verdict["resolved_units"] == 0
    assert verdict["n_tests"] == 2 and verdict["ebh_threshold"] == pytest.approx(40.0)
    assert verdict["horizon_days"] == 21 and verdict["min_calls_for_state"] == 20
    assert verdict["n_unscored"] == 0
    assert _type(verdict, "regime")["n_needed"] is None
    assert _type(verdict, "mandates")["n_needed"] is None  # not benchmarked, no family
    assert "No daily regime calls recorded yet" in _type(verdict, "regime")["note"]
    assert "ADR 0018" in verdict["method_note"] and "about 3,500" in verdict["legacy_method_note"]

    tests = verdict["daily_tests"]
    assert tests["start"] == "2026-10-12" and tests["threshold"] == pytest.approx(40.0)
    assert [(f["family"], f["series"], f["role"]) for f in tests["families"]] == [
        ("F1", "ideas", "primary"), ("F2", "ranking", "primary"), ("F3", "advisor", "secondary"),
    ]
    for f in tests["families"]:
        assert f["n_days"] == 0 and f["state"] == "too_early" and f["posterior"] is None
        assert f["e_skill"] == 1.0 and f["e_harm"] == 1.0 and f["path"] == []
    f1 = _family(verdict, "F1")
    # +1 % per 21 days with 5 % basket noise: the median is many years away, and stated as a range.
    ttk = f1["time_to_know"]
    assert ttk["assumption_unit"] == "excess_per_21d" and ttk["assumption"] == 0.01
    assert ttk["q10_days"] < ttk["q50_days"] < (ttk["q90_days"] or 10**9)
    assert 12 < ttk["q50_years"] < 25
    assert [w["years"] for w in ttk["p_within"]] == [5, 10, 20]
    f2 = _family(verdict, "F2")["time_to_know"]
    assert f2["assumption_unit"] == "rank_ic" and f2["assumption"] == 0.07 and f2["q50_years"] < 4


def test_headline_counts_trading_days_and_states_the_time_to_know_range():
    db = _memory_db()
    user = _user(db)
    _seed_daily(db, user, "ideas", _daily(0.0, 12))

    verdict = build_verdict(db, user.id, now=NOW)
    f1 = _family(verdict, "F1")

    assert verdict["state"] == "too_early" and f1["n_days"] == 12
    assert verdict["headline"].startswith(
        "Too early to tell: 12 trading days so far; an edge of +1 % per 21 days would most likely show after about"
    )
    assert "10-90 % range" in verdict["headline"]
    assert f1["posterior"]["n"] == 12 and 0.0 < f1["posterior"]["p_positive"] < 1.0
    assert _type(verdict, "ideas")["state"] == "too_early" and _type(verdict, "ideas")["family"] == "F1"


def test_days_before_the_start_are_shown_but_never_bet_on():
    db = _memory_db()
    user = _user(db)
    _seed_daily(db, user, "ideas", [0.01] * 30, first=date(2026, 8, 3))

    f1 = _family(build_verdict(db, user.id, now=NOW), "F1")

    assert f1["n_days"] == 0 and f1["e_skill"] == 1.0
    assert f1["pre_registration"]["n_days"] == 30
    assert f1["pre_registration"]["mean_21d"] == pytest.approx(0.21)


def test_days_with_nothing_open_carry_no_bet():
    db = _memory_db()
    user = _user(db)
    _seed_daily(db, user, "ideas", [None, None, 0.002, None])

    f1 = _family(build_verdict(db, user.id, now=NOW), "F1")

    assert f1["n_days"] == 1 and f1["n_empty_days"] == 3


def test_a_strong_daily_record_shows_evidence_of_skill():
    db = _memory_db()
    user = _user(db)
    _seed_daily(db, user, "ideas", _daily(0.003, 300))

    verdict = build_verdict(db, user.id, now=NOW)
    f1 = _family(verdict, "F1")

    assert f1["state"] == "skill" and f1["e_skill"] >= 40
    assert verdict["state"] == "skill" and _type(verdict, "ideas")["state"] == "skill"
    assert verdict["headline"].startswith("Evidence that Discover's picks beat your ETF: e = ")
    assert "over 300 trading days" in verdict["headline"]
    assert f1["posterior"]["p_positive"] > 0.95  # shown, but it is the e-value that decided
    assert f1["time_to_know"]["sd_measured"] is True
    assert len(f1["path"]) <= 261 and f1["path"][-1]["e_skill"] == pytest.approx(f1["e_skill"])
    assert f1["rho1"] is not None


def test_a_losing_daily_record_shows_evidence_of_harm():
    db = _memory_db()
    user = _user(db)
    _seed_daily(db, user, "ideas", _daily(-0.003, 300))

    verdict = build_verdict(db, user.id, now=NOW)

    assert _family(verdict, "F1")["state"] == "harm"
    assert verdict["state"] == "harm" and verdict["headline"].startswith("Evidence of harm")


def test_a_flat_daily_record_after_three_months_is_no_evidence():
    db = _memory_db()
    user = _user(db)
    _seed_daily(db, user, "ideas", _daily(0.0, 80, sd=0.01))

    verdict = build_verdict(db, user.id, now=NOW)

    assert _family(verdict, "F1")["state"] == "no_evidence"
    assert verdict["headline"].startswith("No evidence yet that the picks beat your ETF: 80 trading days so far")


def test_each_family_is_tested_on_its_own_and_only_the_picks_set_the_headline():
    db = _memory_db()
    user = _user(db)
    _seed_daily(db, user, "advisor", _daily(-0.003, 300))
    _seed_daily(db, user, "ranking", _daily(0.003, 300))

    verdict = build_verdict(db, user.id, now=NOW)

    assert _family(verdict, "F3")["state"] == "harm" and _type(verdict, "advisor")["state"] == "harm"
    assert _family(verdict, "F2")["state"] == "skill"
    # F2 and F3 never set the headline: the picks have no record yet.
    assert verdict["state"] == "too_early" and _type(verdict, "ideas")["state"] == "too_early"


def test_the_legacy_per_date_test_is_secondary_and_bounded_by_the_calls_in_flight():
    db = _memory_db()
    user = _user(db)
    _seed_excess(db, user, 500, **SKILL)
    for i in range(500, 520):
        _pred(db, user, i, hit=None, status="pending", resolve_in_days=10)
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)
    ideas = _type(verdict, "ideas")

    assert ideas["e_skill"] >= 40 and ideas["e_skill_crossed_strong"] is True
    # Twenty pending dates at their worst pull the bound below the current value.
    assert ideas["e_skill_lower"] < ideas["e_skill"]
    assert ideas["e_harm_lower"] >= ideas["e_harm"]
    # It sets no state: the calendar-time ledger is empty.
    assert ideas["state"] == "too_early" and verdict["state"] == "too_early"
    skill = verdict["skill"]
    assert skill["metric"] == "mean_excess_return_per_date" and skill["n"] == 500
    assert skill["value"] == pytest.approx(0.9 * 0.12 + 0.1 * -0.02)
    assert "EUNL.DE" in skill["benchmark_label"]


def test_without_calls_in_flight_the_legacy_bound_is_the_current_value():
    db = _memory_db()
    user = _user(db)
    _seed_excess(db, user, 60, **HARM)

    ideas = _type(build_verdict(db, user.id, now=NOW), "ideas")

    assert ideas["e_skill_lower"] == pytest.approx(ideas["e_skill"])
    assert ideas["e_harm_lower"] == pytest.approx(ideas["e_harm"])


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
    # CORP: one PAV block per stated level here, each exactly as reliable as stated.
    assert [(b["p_mean"], b["hit_rate"], b["n"]) for b in ideas["reliability"]] == [
        (pytest.approx(0.3), pytest.approx(0.3), 50), (pytest.approx(0.8), pytest.approx(0.8), 50),
    ]
    corp = ideas["corp"]
    assert corp["mcb"] == pytest.approx(0.0, abs=1e-12) and corp["dsc"] > 0
    assert ideas["brier"] == pytest.approx(corp["mcb"] - corp["dsc"] + corp["unc"])
    assert ideas["n_eff_stated"] == 100  # one call per date here
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
    # Far fewer than 30 matured weeks: the conformal correction has not started.
    aci = ideas_cov["aci"]
    assert aci["active"] is False and aci["widen_now"] is None and aci["corrected"] is None
    assert aci["weeks_needed"] == 30 and 0 < aci["matured_weeks"] < 30
    assert sum(d["n"] for d in aci["per_date"]) == 20


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
    _seed_excess(db, user, 500, **SKILL, p=0.9)
    _seed_daily(db, user, "ideas", _daily(0.003, 300))

    body = http.get("/api/trust/verdict").json()

    assert body["state"] == "skill"
    ideas = body["types"][0]
    assert ideas["n"] == 500 and ideas["hit_ci"][0] < 0.9 < ideas["hit_ci"][1]
    assert ideas["calibration_caption"] == "When we said 90 %, it happened 450 of 500 times."
    assert body["skill"]["n"] == 500
    assert body["ebh_threshold"] == pytest.approx(40.0) and body["min_calls_for_state"] == 20
    f1 = body["daily_tests"]["families"][0]
    assert f1["state"] == "skill" and f1["posterior"]["sensitivity"][0]["tau_21d"] == 0.0025


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


def test_resolved_calls_without_a_benchmark_price_are_counted_not_dropped():
    db = _memory_db()
    user = _user(db)
    for i in range(3):
        _pred(db, user, i, hit=True)
    row = _pred(db, user, 3, hit=True)
    row.excess_return = None  # resolved, but the benchmark had no price
    hold = _pred(db, user, 4, hit=True, direction="neutral")
    assert hold is not None
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)
    ideas = _type(verdict, "ideas")

    assert ideas["n_unscored"] == 1 and verdict["n_unscored"] == 1
    assert ideas["n_holds"] == 1
    assert ideas["n_calls"] == 3


def test_time_to_know_follows_the_issue_cadence_weekly_ideas_daily_advisor():
    db = _memory_db()
    user = _user(db)
    sleeve = _sleeve(db, user)
    for i in range(12):  # ideas: one date per week
        row = _pred(db, user, i, hit=True)
        row.predicted_at = START + timedelta(weeks=i)
    for i in range(12):  # advisor: one date per trading day
        _pred(db, user, i, hit=True, sleeve=sleeve)
    db.commit()

    verdict = build_verdict(db, user.id, now=NOW)
    ideas, advisor = _type(verdict, "ideas"), _type(verdict, "advisor")

    assert ideas["cadence_days"] == 5.0 and ideas["h_eff"] == 5
    assert ideas["n_needed"] == 868
    assert ideas["years_needed"] == pytest.approx(868 * 5 / 252)
    assert ideas["lag_h"] == 5  # chains the actual weekly schedule needs
    assert advisor["cadence_days"] == 1.0 and advisor["h_eff"] == 21
    assert advisor["n_needed"] == 3503 and advisor["lag_h"] == 12  # 12 dates need 12 chains so far
    assert advisor["years_needed"] == pytest.approx(3503 / 252)
    assert verdict["n_needed"] == 868 and verdict["years_needed"] > 0  # the picks' own schedule
