"""The capital ladder (plan 2026-11-10 WS4, ADR 0025)."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest

from conftest import _memory_db

from app.decision.graduation import ladder as lad
from app.foundation.models.entities import (
    AdvisorGraduationTransition,
    LadderCandidate,
    LadderEvidenceDaily,
    PaperPortfolio,
    PaperSnapshot,
    User,
)

DAILY = math.sqrt(252)


def _step_path(
    x: np.ndarray, *, start_stage: int = 0, epoch: int = 1, drawdown_band: bool = True
) -> tuple[int, str | None, int | None]:
    """Run the stage machine over a path. Returns (best stage, demotion reason, demotion day)."""
    sigma = lad.lagged_sigma(x)
    e, h = lad.e_processes(x, sigma)
    dd = lad.relative_drawdown(x)
    earned, proven = lad.thresholds("llm", epoch)
    n = np.arange(1, len(x) + 1)
    csum, csq = np.cumsum(x), np.cumsum(x * x)
    var = np.where(n > 1, (csq - csum**2 / n) / np.maximum(n - 1, 1), np.nan)
    te = np.sqrt(np.maximum(var, 0.0)) * DAILY
    stage, best = start_stage, start_stage
    for i in range(len(x)):
        t = float(te[i]) if i > 0 else None
        inputs = lad.StageInputs(
            days=i + 1, e_skill=float(e[i]), e_harm=float(h[i]), cum_x=float(csum[i]), rel_dd=float(dd[i]),
            dd_band=lad.drawdown_band(t, i + 1) if drawdown_band else None,
            null_band=(lad.NULL_BAND_Z * t / DAILY * math.sqrt(i + 1)) if t else None,
            brier=None, vetoed=False, earned_at=earned, proven_at=proven,
        )
        stage, reason, new_epoch = lad.stage_for(stage, inputs)
        if new_epoch:
            return best, reason, i
        best = max(best, stage)
    return best, None, None


# --- Pre-registered numbers -----------------------------------------------------------


def test_epoch_thresholds_are_10_and_40_then_rise():
    assert lad.thresholds("llm", 1) == pytest.approx((10.0, 40.0))
    assert lad.thresholds("rules", 1) == pytest.approx((10.0, 40.0))
    assert lad.thresholds("llm", 2) == pytest.approx((40.0, 160.0))
    assert lad.thresholds("llm", 3) == pytest.approx((90.0, 360.0))
    assert lad.LAMBDAS == (0.025, 0.05, 0.10, 0.20, 0.35)
    assert (lad.TUITION_MIN_DAYS, lad.EWMA_HALFLIFE_DAYS, lad.HARM_THRESHOLD) == (126, 126, 10.0)


def test_tuition_weight():
    assert lad.tuition_weight_pct(0.10) == pytest.approx(5.0)  # capped
    assert lad.tuition_weight_pct(0.103) == pytest.approx(5.0, abs=0.02)
    assert lad.tuition_weight_pct(0.14) == pytest.approx(100 * 0.01 / (0.03 + 1.645 * 0.14))  # ~3.8 %
    assert lad.tuition_weight_pct(0.14) == pytest.approx(3.84, abs=0.01)
    assert lad.tuition_weight_pct(None) == 0.0


# --- Simulations (fixed seed 20261110, the one pre-registered for the Pocock constant) ---


def test_null_candidate_rarely_reaches_earned():
    """alpha = 0 over 5 years of daily data: at most 5 % of 500 paths reach Earned."""
    rng = np.random.default_rng(20261110)
    best = [_step_path(rng.normal(0.0, 0.06 / DAILY, 1260))[0] for _ in range(500)]
    assert np.mean(np.array(best) >= 2) <= 0.05


def test_planted_alpha_reaches_earned():
    rng = np.random.default_rng(20261110)
    best = [_step_path(rng.normal(0.12 / 252, 0.06 / DAILY, 1260))[0] for _ in range(30)]
    assert np.mean(np.array(best) >= 2) >= 0.8


def test_harm_e_process_demotes_a_planted_loser():
    rng = np.random.default_rng(20261110)
    # The harm e-process alone (drawdown band off) demotes most planted losers
    # within five years; with the band on, every one is demoted.
    paths = [rng.normal(-0.08 / 252, 0.06 / DAILY, 1260) for _ in range(30)]
    harm = [_step_path(x, start_stage=1, drawdown_band=False)[1] for x in paths]
    assert sum(1 for r in harm if r and r.startswith("harm")) >= 0.6 * len(paths)
    both = [_step_path(x, start_stage=1)[1] for x in paths]
    assert sum(1 for r in both if r is not None) >= sum(1 for r in harm if r is not None)


def _inputs(**kw) -> lad.StageInputs:
    base = dict(days=200, e_skill=1.0, e_harm=1.0, cum_x=0.0, rel_dd=0.0, dd_band=0.2, null_band=0.1,
                brier=None, vetoed=False, earned_at=10.0, proven_at=40.0)
    base.update(kw)
    return lad.StageInputs(**base)


def test_drawdown_band_demotes_to_a_new_epoch():
    stage, reason, new_epoch = lad.stage_for(2, _inputs(e_skill=15.0, rel_dd=0.25, dd_band=0.2))
    assert (stage, new_epoch) == (0, True) and "drawdown" in reason


def test_tuition_needs_126_days():
    assert lad.stage_for(0, _inputs(days=125))[0] == 0
    assert lad.stage_for(0, _inputs(days=126))[0] == 1


def test_calibration_and_null_band_veto_tuition():
    assert lad.stage_for(0, _inputs(brier=0.30))[0] == 0
    assert lad.stage_for(0, _inputs(cum_x=-0.2, null_band=0.1))[0] == 0
    assert lad.stage_for(0, _inputs(vetoed=True))[0] == 0


def test_stage_moves_up_and_back():
    assert lad.stage_for(1, _inputs(e_skill=10.5))[0] == 2
    assert lad.stage_for(2, _inputs(e_skill=45.0))[0] == 3
    assert lad.stage_for(3, _inputs(e_skill=5.0))[0] == 3  # holds above 1
    assert lad.stage_for(3, _inputs(e_skill=0.9))[:2] == (1, "skill e-value 0.90 fell below 1")
    assert lad.stage_for(1, _inputs(vetoed=True)) == (0, "owner veto", True)


# --- Database: candidates, nightly run, monthly weight --------------------------------------


def _user(db) -> User:
    user = User(id=uuid4().hex, username="jan", password_hash="x")
    db.add(user)
    db.commit()
    return user


def _mandate_b(db, user, born: datetime, x: np.ndarray, bench: dict[str, float]) -> PaperPortfolio:
    portfolio = PaperPortfolio(user_id=user.id, mandate="B", name="Mandate B", managed_by="llm",
                               initial_cash=Decimal("100000"), inception_at=born, created_at=born)
    db.add(portfolio)
    db.flush()
    days = sorted(date.fromisoformat(k) for k in bench)
    nav = 100_000.0
    db.add(PaperSnapshot(portfolio_id=portfolio.id, date=days[0], total_value=Decimal(str(nav))))
    for i, (prev, cur) in enumerate(zip(days, days[1:])):
        r_b = bench[cur.isoformat()] / bench[prev.isoformat()] - 1
        nav *= 1 + r_b + x[i]
        db.add(PaperSnapshot(portfolio_id=portfolio.id, date=cur, total_value=Decimal(str(round(nav, 4)))))
    db.commit()
    return portfolio


def _bench(start: date, n: int, seed: int = 1) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    out, price, d = {}, 50.0, start
    while len(out) < n:
        if d.weekday() < 5:
            out[d.isoformat()] = price
            price *= 1 + rng.normal(0.0003, 0.01)
        d += timedelta(days=1)
    return out


def test_run_ladder_registers_measures_and_reaches_tuition():
    db = _memory_db()
    user = _user(db)
    born = datetime(2026, 1, 5, tzinfo=UTC)
    bench = _bench(born.date(), 200)
    x = np.random.default_rng(5).normal(0.0, 0.04 / DAILY, 199)
    portfolio = _mandate_b(db, user, born, x, bench)
    last_day = max(date.fromisoformat(k) for k in bench)
    now = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), UTC)

    state = lad.run_ladder(db, user.id, now, bench_closes=bench)

    cand = db.query(LadderCandidate).one()
    assert (cand.slot, cand.kind, cand.portfolio_id, cand.epoch) == ("rules", "mandate", portfolio.id, 1)
    rows = db.query(LadderEvidenceDaily).filter_by(candidate_id=cand.id).all()
    assert len(rows) == 199
    assert cand.stage == 1  # 199 paired days, no harm: Tuition
    assert db.query(AdvisorGraduationTransition).count() == 1
    [card] = state["candidates"]
    assert card["stage_name"] == "Tuition" and card["days_of_evidence"] == 199
    assert card["thresholds"] == {"earned": 10.0, "proven": 40.0, "harm": 10.0}
    assert state["estimate"] and state["not_tax_advice"] and state["not_financial_advice"]

    # Idempotent: a second run the same night adds no rows and no transitions.
    lad.run_ladder(db, user.id, now, bench_closes=bench)
    assert db.query(LadderEvidenceDaily).filter_by(candidate_id=cand.id).count() == 199
    assert db.query(AdvisorGraduationTransition).count() == 1


def test_stage_change_takes_effect_at_the_next_monthly_plan():
    db = _memory_db()
    user = _user(db)
    born = datetime(2026, 1, 5, tzinfo=UTC)
    bench = _bench(born.date(), 200)
    x = np.random.default_rng(5).normal(0.0, 0.04 / DAILY, 199)
    _mandate_b(db, user, born, x, bench)
    last_day = max(date.fromisoformat(k) for k in bench)
    lad.run_ladder(db, user.id, datetime.combine(last_day + timedelta(days=1), datetime.min.time(), UTC),
                   bench_closes=bench)
    cand = db.query(LadderCandidate).one()
    first_tuition = min(r.day for r in db.query(LadderEvidenceDaily).filter_by(candidate_id=cand.id)
                        if r.stage == 1)
    # Within the month Tuition began: still 0.
    assert lad.ladder_weight(db, user.id, today=first_tuition + timedelta(days=1)) == 0.0 or (
        first_tuition.month != (first_tuition + timedelta(days=1)).month
    )
    # From the first of the next month: the Tuition weight.
    next_month = (first_tuition.replace(day=1) + timedelta(days=32)).replace(day=1)
    weight = lad.ladder_weight(db, user.id, today=next_month)
    assert 0.0 < weight <= 0.05


def test_authorisation_expires_after_24_months_as_a_new_epoch():
    db = _memory_db()
    user = _user(db)
    born = datetime(2024, 1, 2, tzinfo=UTC)
    portfolio = PaperPortfolio(user_id=user.id, mandate="B", name="B", managed_by="llm",
                               initial_cash=Decimal("1"), inception_at=born, created_at=born)
    db.add(portfolio)
    db.commit()
    first = lad.register_candidate(db, user.id, "rules", now=born + timedelta(days=10))
    assert first is not None and first.epoch == 1
    same = lad.register_candidate(db, user.id, "rules", now=born + timedelta(days=700))
    assert same is not None and same.id == first.id
    later = lad.register_candidate(db, user.id, "rules", now=born + timedelta(days=740))
    assert later is not None and later.epoch == 2 and later.pi == pytest.approx(0.125)
    db.refresh(first)
    assert first.retired_at is not None


def test_a_paper_reset_is_a_new_epoch():
    db = _memory_db()
    user = _user(db)
    born = datetime(2026, 1, 2, tzinfo=UTC)
    portfolio = PaperPortfolio(user_id=user.id, mandate="B", name="B", managed_by="llm",
                               initial_cash=Decimal("1"), inception_at=born, created_at=born)
    db.add(portfolio)
    db.commit()
    first = lad.register_candidate(db, user.id, "rules", now=born + timedelta(days=1))
    portfolio.inception_at = born + timedelta(days=50)
    db.commit()
    second = lad.register_candidate(db, user.id, "rules", now=born + timedelta(days=51))
    assert first is not None and second is not None
    assert second.epoch == 2 and second.born_at.date() == (born + timedelta(days=50)).date()


def _candidate_at(db, user, stage: int, te: float, day: date) -> None:
    cand = LadderCandidate(user_id=user.id, slot="rules", kind="mandate", ref_id="p", portfolio_id="p",
                           config_hash="h", epoch=1, pi=0.5, born_at=datetime(2025, 1, 2, tzinfo=UTC), stage=stage)
    db.add(cand)
    db.flush()
    db.add(LadderEvidenceDaily(candidate_id=cand.id, day=day, x=0.0, te=te, e_skill=2.0, e_harm=1.0, stage=stage))
    db.commit()


@pytest.mark.parametrize(
    ("stage", "te", "cap", "expected"),
    [
        (0, 0.08, 20, 0.0),
        (1, 0.08, 20, 5.0),  # TE <= 10.3 %: the 5 % cap
        (1, 0.14, 20, 100 * 0.01 / (0.03 + 1.645 * 0.14)),  # about 3.8 %
        (2, 0.08, 20, 10.0),
        (3, 0.08, 20, 15.0),
        (3, 0.08, 12, 12.0),  # never above plan_satellite_max_pct
    ],
)
def test_monthly_plan_satellite_follows_the_ladder(stage, te, cap, expected):
    from app.decision import monthly_plan
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"plan_satellite_max_pct": cap})
    _candidate_at(db, user, stage, te, date(2026, 10, 30))
    evidence = monthly_plan.evidence_state(db, datetime(2026, 11, 10, tzinfo=UTC), user_id=user.id)
    targets, unlocked = monthly_plan.sleeve_targets_from(monthly_plan.plan_settings(db), evidence)
    assert targets["satellite"] == pytest.approx(expected, abs=1e-6)
    assert unlocked["satellite"] is (expected > 0)


def test_monthly_plan_ignores_a_stage_reached_this_month():
    from app.decision import monthly_plan

    db = _memory_db()
    user = _user(db)
    _candidate_at(db, user, 2, 0.08, date(2026, 11, 3))  # reached after 1 Nov
    evidence = monthly_plan.evidence_state(db, datetime(2026, 11, 10, tzinfo=UTC), user_id=user.id)
    assert evidence["satellite_target_pct"] == 0.0
    evidence = monthly_plan.evidence_state(db, datetime(2026, 12, 1, tzinfo=UTC), user_id=user.id)
    assert evidence["satellite_target_pct"] == 10.0


def test_plan_ceiling_default_is_20_and_paper_budget_stays_10():
    from app.decision import monthly_plan

    db = _memory_db()
    assert monthly_plan.plan_settings(db)["satellite_max_pct"] == 20.0
    assert monthly_plan.paper_plan_targets(db)["satellite_max"] == pytest.approx(0.10)
