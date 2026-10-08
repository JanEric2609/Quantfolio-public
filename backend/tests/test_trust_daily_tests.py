"""The calendar-time test statistics of ADR 0018 (§4 bettor, §7 posterior, the time-to-know simulator)."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification import daily_tests
from app.decision.verification.trust import build_verdict
from app.foundation import forecast_verification as fv
from app.foundation.core.db import Base
from app.foundation.models.entities import DiscoveryPrediction, User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_the_pre_registered_constants_match_the_adr():
    assert daily_tests.CLIP == 0.05
    assert daily_tests.SIGMA_REF == pytest.approx(0.0109, abs=1e-4)
    assert daily_tests.PRIOR_VAR == pytest.approx((daily_tests.SIGMA_REF / (2 * daily_tests.CLIP)) ** 2, rel=0.03)  # 1.1 % rounded
    assert daily_tests.BET_CAP == 0.75
    assert daily_tests.THRESHOLD == pytest.approx(40.0)
    assert daily_tests.TAU_21D == 0.005
    assert daily_tests.PRIMARY_TEST_START.isoformat() == "2026-10-12"


def test_the_bet_cap_keeps_every_factor_at_or_above_a_quarter():
    # A long winning run pushes the bet to the cap; one total loss then costs at most 75 %.
    proc = fv.e_process_bernoulli([1.0] * 200 + [0.0], 0.5, 0.0121, cap_factor=0.75)
    assert proc.path[-1] / proc.path[-2] == pytest.approx(0.25)
    with pytest.raises(ValueError):
        fv.e_process_bernoulli([0.5], 0.5, cap_factor=1.0)


def test_e_bh_with_two_hypotheses_needs_forty():
    assert fv.e_bh([39.0, 1.0], 0.05) == [False, False]
    assert fv.e_bh([40.0, 1.0], 0.05) == [True, False]


def test_the_posterior_shrinks_the_sample_mean_towards_zero():
    x = np.full(100, 0.001) + np.tile([0.01, -0.01], 50)
    post = fv.normal_posterior(x, tau=0.0005, lag=0)
    se2 = fv.hac_variance_of_mean(x, 0)
    kappa = 0.0005**2 / (0.0005**2 + se2)
    assert post.shrinkage == pytest.approx(kappa)
    assert post.mean == pytest.approx(kappa * 0.001)
    assert 0.5 < post.p_positive < 1.0
    assert post.ci_low < post.mean < post.ci_high
    # A wider prior trusts the data more.
    wide = fv.normal_posterior(x, tau=0.01, lag=0)
    assert wide.shrinkage > post.shrinkage and wide.mean > post.mean
    assert fv.normal_posterior([0.1], tau=0.01, lag=0) is None


def test_a_floor_on_the_standard_error_guards_tiny_samples():
    x = [0.002, 0.0021, 0.0019]
    loose = fv.normal_posterior(x, tau=0.0005, lag=0)
    floored = fv.normal_posterior(x, tau=0.0005, lag=0, se_floor=0.011 / math.sqrt(3))
    assert floored.p_positive < loose.p_positive


def test_newey_west_lag_and_autocorrelation_helpers():
    assert fv.newey_west_lag(0) == 0 and fv.newey_west_lag(100) == 4 and fv.newey_west_lag(1000) == 6
    assert fv.lag1_autocorrelation([1.0, 2.0]) is None
    assert fv.lag1_autocorrelation(np.tile([1.0, -1.0], 20)) == pytest.approx(-0.975, abs=0.01)


def test_time_to_know_is_a_seeded_distribution_that_shortens_with_the_edge():
    kw = dict(prior_var=daily_tests.PRIOR_VAR, cap_factor=0.75, within=(252, 1260), n_max=4000, n_paths=200)
    small = fv.days_to_verdict(0.08, 0.011, 0.05, 40.0, **kw)
    large = fv.days_to_verdict(0.16, 0.011, 0.05, 40.0, **kw)
    assert fv.days_to_verdict(0.08, 0.011, 0.05, 40.0, **kw) == small
    assert small.q10 < small.q50 < small.q90
    assert large.q50 < small.q50
    # Roughly 1 / edge^2: twice the edge, about a quarter of the time.
    assert 0.15 < large.q50 / small.q50 < 0.4
    assert small.p_within[252] <= small.p_within[1260]
    with pytest.raises(ValueError):
        fv.days_to_verdict(0.0, 0.011, 0.05, 40.0, prior_var=0.01)


def test_first_resolution_due_is_reported_while_nothing_has_resolved():
    db = _memory_db()
    user = User(username="due", password_hash="x")
    db.add(user)
    db.commit()
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    for days in (14, 6):
        db.add(DiscoveryPrediction(
            user_id=user.id, run_id="r", symbol="AAA", predicted_at=now - timedelta(days=1), horizon_days=21,
            resolve_at=now + timedelta(days=days), direction="buy", conviction=0.5, outcome_status="pending",
            features_json={},
        ))
    db.commit()

    verdict = build_verdict(db, user.id, now=now)

    assert verdict["first_resolution_due"].startswith((now + timedelta(days=6)).date().isoformat())
    ideas = next(t for t in verdict["types"] if t["type"] == "ideas")
    assert ideas["e_skill_lower"] == pytest.approx(1.0)


def test_corp_pools_adjacent_violators_and_decomposes_the_brier_score():
    p = [0.1, 0.2, 0.3, 0.4]
    y = [0.0, 1.0, 0.0, 1.0]
    fit = fv.corp_reliability(p, y)
    # 0.2 hit and 0.3 missed: the violators pool into one block at 0.5.
    assert [round(pt["hit_rate"], 3) for pt in fit.points] == [0.0, 0.5, 1.0]
    assert fit.mcb >= 0 and fit.dsc >= 0
    assert fv.brier_score(p, y) == pytest.approx(fit.mcb - fit.dsc + fit.unc)
    assert fv.corp_reliability([], []) is None


def test_clustered_z_counts_dates_not_calls():
    # Ten calls on each of two dates, all at 0.7; one date all hit, one all missed.
    p = [0.7] * 20
    y = [1.0] * 10 + [0.0] * 10
    groups = ["d1"] * 10 + ["d2"] * 10
    naive = fv.spiegelhalter_z(p, y)
    clustered = fv.spiegelhalter_z_clustered(p, y, groups)
    assert clustered is not None and abs(clustered) < abs(naive)  # two dates, not twenty calls
    assert fv.spiegelhalter_z_clustered(p, y, ["d1"] * 20) is None


def test_brier_skill_bootstraps_whole_blocks_of_dates():
    rng = np.random.default_rng(0)
    p = list(rng.uniform(0.2, 0.8, 200))
    y = [float(rng.uniform() < pi) for pi in p]
    per_call = fv.brier_skill(p, y)
    by_block = fv.brier_skill(p, y, blocks=[i // 20 for i in range(200)])
    assert per_call.skill == pytest.approx(by_block.skill)
    assert by_block.ci_low is not None and by_block.ci_low <= by_block.skill <= by_block.ci_high
    assert fv.brier_skill(p, y, blocks=[0] * 200).ci_low is None  # one block: no interval


def test_paired_comparison_uses_only_common_days_after_the_start():
    from datetime import date

    from app.foundation.models.entities import TrustDailyActiveReturn

    start = daily_tests.PRIMARY_TEST_START

    def row(series: str, day: date, value: float | None, n_open: int = 3) -> TrustDailyActiveReturn:
        return TrustDailyActiveReturn(
            user_id="u", series=series, day=day, n_open=n_open, value=value, benchmark="EUNL.DE"
        )

    days = [start + timedelta(days=i) for i in range(10)]
    ideas = [row("ideas", d, 0.001) for d in days[:8]]
    advisor = [row("advisor", d, 0.003) for d in days[2:]]
    advisor.append(row("advisor", days[3] - timedelta(days=30), 0.5))  # before T0: never counted
    ideas.append(row("ideas", days[3] - timedelta(days=30), 0.0))
    ideas.append(row("ideas", days[9], None, n_open=0))  # nothing open that day

    out = daily_tests.paired_comparison(ideas, advisor)
    assert out["n_days"] == 6  # days 2..7
    assert out["n_days_pre_registration"] == 1
    assert out["n_days_advisor_only"] == 2 and out["n_days_ideas_only"] == 2
    assert out["mean_21d"] == pytest.approx(0.002 * 21)
    assert out["share_advisor_ahead"] == 1.0
    assert out["enough_days"] is False
    assert out["path"][-1]["cumulative"] == pytest.approx(0.012)


def test_paired_comparison_is_empty_without_common_days():
    out = daily_tests.paired_comparison([], [])
    assert out["n_days"] == 0 and out["mean_21d"] is None and out["mean_ci_21d"] is None and out["path"] == []
