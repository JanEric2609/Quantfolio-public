"""Champion/challenger evolution tests (PR2 group C).

Acceptance: a challenger seeded to outperform on the 4 axes gets promoted;
one that underperforms is discarded (and a fresh mutation spawned); a
marginal winner within the small-sample guard is NOT promoted; rounds are
recorded as CompetitionRun/CompetitionDecision so graduation's win-rate
keeps working.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorScorecard,
    CompetitionDecision,
    CompetitionRun,
    DiscoveryPrediction,
    PaperSnapshot,
    User,
)
from app.foundation.models.entities import TrialLedgerEntry
from app.decision.advisor.evolution import composite_score, run_evolution_round
from app.decision.advisor.strategy import (
    ensure_champion,
    get_active_challenger,
    get_champion,
    promote_challenger,
    spawn_challenger,
)
from app.foundation.quant_metrics import resolve_n_trials


def _user(db, name="jan") -> User:
    user = User(id=uuid4().hex, username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _seed_sleeve(db, user: User, portfolio_id: str, *, good: bool, n_preds: int = 6) -> None:
    """Seed NAV history + resolved predictions so the 4 axes are computable.

    good=True  → rising NAV, honest calibration, accurate magnitudes, no DD.
    good=False → falling NAV, overconfident misses, inverted magnitudes.
    """
    today = date.today()
    for i in range(30):
        nav = 100_000 * (1 + (0.004 if good else -0.01)) ** i
        db.add(
            PaperSnapshot(
                id=uuid4().hex, portfolio_id=portfolio_id,
                date=today - timedelta(days=30 - i),
                total_value=Decimal(str(round(nav, 2))),
            )
        )
    now = datetime.now(UTC)
    for i in range(n_preds):
        predicted = 0.02 + i * 0.01
        realised = predicted if good else -predicted
        db.add(
            DiscoveryPrediction(
                id=uuid4().hex, user_id=user.id, run_id="evo",
                portfolio_id=portfolio_id, symbol=f"S{i}",
                predicted_at=now - timedelta(days=25), horizon_days=21,
                resolve_at=now - timedelta(days=2), direction="buy",
                conviction=0.8 if good else 0.9,
                expected_return=predicted, realised_return=realised,
                outcome_status="resolved",
            )
        )
    db.commit()


def test_outperforming_challenger_is_promoted():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=1)

    _seed_sleeve(db, user, champion.portfolio_id, good=False)
    _seed_sleeve(db, user, challenger.portfolio_id, good=True)

    summary = run_evolution_round(db, user.id, min_resolved=5, reflect=False)
    assert summary["verdict"] == "promote"
    assert summary["promoted_strategy_id"] == challenger.id

    db.refresh(challenger)
    assert challenger.role == "champion"
    assert get_champion(db, user.id).id == challenger.id
    # A fresh challenger was spawned to keep the arena running.
    new_challenger = get_active_challenger(db, user.id)
    assert new_challenger is not None and new_challenger.id != challenger.id

    # Round recorded for graduation's win-rate.
    run = db.query(CompetitionRun).filter(CompetitionRun.name == "Champion vs Challenger").one()
    decisions = db.query(CompetitionDecision).filter(CompetitionDecision.run_id == run.id).all()
    assert len(decisions) == 2
    winners = [d for d in decisions if d.winner]
    assert len(winners) == 1 and winners[0].portfolio_id == challenger.portfolio_id


def test_underperforming_challenger_is_discarded():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=2)

    _seed_sleeve(db, user, champion.portfolio_id, good=True)
    _seed_sleeve(db, user, challenger.portfolio_id, good=False)

    summary = run_evolution_round(db, user.id, min_resolved=5, reflect=False)
    assert summary["verdict"] == "retire_challenger"

    db.refresh(challenger)
    db.refresh(champion)
    assert challenger.role == "retired" and challenger.retired_at is not None
    assert champion.role == "champion"
    # A new mutation was spawned.
    new_challenger = get_active_challenger(db, user.id)
    assert new_challenger is not None and new_challenger.id != challenger.id


def test_marginal_winner_is_not_promoted():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=3)

    # Identical performance → inside the promotion margin.
    _seed_sleeve(db, user, champion.portfolio_id, good=True)
    _seed_sleeve(db, user, challenger.portfolio_id, good=True)

    summary = run_evolution_round(db, user.id, min_resolved=5, reflect=False)
    assert summary["verdict"] == "marginal_keep_observing"
    db.refresh(champion)
    db.refresh(challenger)
    assert champion.role == "champion"
    assert challenger.role == "challenger"  # keeps competing, no churn


def test_small_sample_blocks_promotion():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=4)

    _seed_sleeve(db, user, champion.portfolio_id, good=False, n_preds=6)
    _seed_sleeve(db, user, challenger.portfolio_id, good=True, n_preds=6)

    # Clear 4-axis win but nowhere near the required decision sample.
    summary = run_evolution_round(db, user.id, min_resolved=100, reflect=False)
    assert summary["verdict"] == "insufficient_sample"
    db.refresh(challenger)
    assert challenger.role == "challenger"  # no lucky-streak promotion


def test_composite_score_withheld_without_enough_axes():
    score, detail = composite_score(None)
    assert score is None and "note" in detail


def test_composite_withheld_when_magnitude_null_even_with_three_other_axes():
    """F15: an all-cash-shaped book aces risk_adjusted/calibration/downside
    (flat, near-zero-vol, no drawdown) with no magnitude forecast at all —
    the composite must NOT silently average over the other 3 axes and
    report a high score (the "0.94/1.0" prod bug); it must stay withheld."""
    scorecard = AdvisorScorecard(
        sharpe=3.0,          # excellent risk_adjusted
        brier_avg=0.01,      # excellent calibration
        max_drawdown=0.0,    # zero downside
        mz_slope=None,       # no magnitude forecast at all
        mz_r2=None,
    )
    score, detail = composite_score(scorecard)
    assert score is None
    assert "magnitude" in detail["note"]
    assert "magnitude" not in detail["components"]


def test_composite_computed_when_all_four_axes_present():
    scorecard = AdvisorScorecard(
        sharpe=1.0, brier_avg=0.15, max_drawdown=0.05, mz_slope=0.9, mz_r2=0.6,
    )
    score, detail = composite_score(scorecard)
    assert score is not None
    assert "magnitude" in detail["components"]


def test_composite_uses_psr_directly_when_present():
    """F11: risk_adjusted uses scorecard.psr directly (already a probability
    in [0,1]) rather than sigmoid(sharpe) when psr is available."""
    scorecard = AdvisorScorecard(
        sharpe=20.0,  # would saturate sigmoid(sharpe) to ~1.0
        psr=0.42,     # deliberately different from what sigmoid(20) would give
        brier_avg=0.15, max_drawdown=0.05, mz_slope=0.9, mz_r2=0.6,
    )
    score, detail = composite_score(scorecard)
    assert score is not None
    assert detail["components"]["risk_adjusted"] == pytest.approx(0.42)


def test_composite_falls_back_to_sigmoid_sharpe_without_psr():
    """Transitional fallback for scorecard rows computed before PSR existed."""
    scorecard = AdvisorScorecard(
        sharpe=0.0, psr=None,
        brier_avg=0.15, max_drawdown=0.05, mz_slope=0.9, mz_r2=0.6,
    )
    score, detail = composite_score(scorecard)
    assert score is not None
    assert detail["components"]["risk_adjusted"] == pytest.approx(0.5)  # sigmoid(0) = 0.5


def test_composite_uses_rps_directly_when_present():
    """F11: calibration uses 1 - scorecard.rps_avg rather than the sign-only
    Brier formula when rps_avg is available."""
    scorecard = AdvisorScorecard(
        sharpe=1.0, psr=0.6,
        brier_avg=0.0,   # would give calibration=1.0 under the old formula
        rps_avg=0.3,      # deliberately different: 1 - 0.3 = 0.7
        max_drawdown=0.05, mz_slope=0.9, mz_r2=0.6,
    )
    score, detail = composite_score(scorecard)
    assert score is not None
    assert detail["components"]["calibration"] == pytest.approx(0.7)


def test_composite_falls_back_to_brier_without_rps():
    """Transitional fallback for scorecard rows computed before rps_avg existed."""
    scorecard = AdvisorScorecard(
        sharpe=1.0, psr=0.6, rps_avg=None,
        brier_avg=0.25,  # coin-flip -> calibration = 1 - 0.25/0.25 = 0.0
        max_drawdown=0.05, mz_slope=0.9, mz_r2=0.6,
    )
    score, detail = composite_score(scorecard)
    assert score is not None
    assert detail["components"]["calibration"] == pytest.approx(0.0)


def test_composite_withheld_when_only_magnitude_and_one_other_axis_present():
    """Magnitude alone isn't sufficient either — still need >= 3 axes total,
    magnitude just can't be one of the ones that's missing."""
    scorecard = AdvisorScorecard(
        sharpe=1.0, brier_avg=None, max_drawdown=None, mz_slope=0.9, mz_r2=0.6,
    )
    score, detail = composite_score(scorecard)
    assert score is None
    assert "magnitude" not in detail["note"]


def test_challenger_config_is_bounded_mutation():
    db = _memory_db()
    user = _user(db)
    ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=5)
    cfg = challenger.config_json
    assert cfg["prompt_framing"]  # a style framing was applied
    assert 5 <= cfg["max_candidates"] <= 15
    from app.decision.advisor.risk_gate import DEFAULT_RISK_CEILINGS

    for key, default_val in DEFAULT_RISK_CEILINGS.items():
        # Never looser than the default envelope (safety), never absurdly tight.
        assert cfg["risk_ceilings"][key] <= default_val + 1e-9
        assert cfg["risk_ceilings"][key] >= default_val * 0.5 - 1e-9


# ---------------------------------------------------------------------------
# ADR 0015 Phase 2: champion promotion registers the trial ledger entry the
# graduation gate's out-of-sample-confirmation criterion reads.
# ---------------------------------------------------------------------------


def test_ensure_champion_registers_a_ledger_entry():
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)

    entry = (
        db.query(TrialLedgerEntry)
        .filter(
            TrialLedgerEntry.context == "advisor_champion",
            TrialLedgerEntry.trial_key == champion.id,
        )
        .one_or_none()
    )
    assert entry is not None
    assert entry.registered_at is not None


def test_ensure_champion_ledger_registration_is_idempotent():
    """Calling ensure_champion again for the same user must not touch the
    ledger a second time — it returns the existing champion untouched."""
    db = _memory_db()
    user = _user(db)
    champion = ensure_champion(db, user.id)
    ensure_champion(db, user.id)

    count = (
        db.query(TrialLedgerEntry)
        .filter(
            TrialLedgerEntry.context == "advisor_champion",
            TrialLedgerEntry.trial_key == champion.id,
        )
        .count()
    )
    assert count == 1


def test_promote_challenger_registers_the_new_champion_in_the_ledger():
    db = _memory_db()
    user = _user(db)
    ensure_champion(db, user.id)
    challenger = spawn_challenger(db, user.id, seed=3)

    promoted = promote_challenger(db, challenger)

    entry = (
        db.query(TrialLedgerEntry)
        .filter(
            TrialLedgerEntry.context == "advisor_champion",
            TrialLedgerEntry.trial_key == promoted.id,
        )
        .one_or_none()
    )
    assert entry is not None


def test_champion_ledger_entries_count_toward_resolve_n_trials():
    """The champion-promotion writes are real rows the resolver counts,
    not silently dropped."""
    db = _memory_db()
    user = _user(db)
    ensure_champion(db, user.id)

    assert resolve_n_trials(db) == 1
    raw_count = db.query(TrialLedgerEntry).count()
    assert raw_count == 1
