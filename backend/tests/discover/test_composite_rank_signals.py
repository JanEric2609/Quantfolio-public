"""WP2a (plan 2026-11-10): every signal is ranked inside its lane before weighting."""
from __future__ import annotations

import random

from app.decision.discover.composite import (
    WEIGHTS,
    rank_signals_within_lanes,
    variance_shares,
)


def _cross_section(n=300, seed=1):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        rows.append({
            "momentum": rng.random(),                      # uniform rank, sd 0.29
            "portfolio": 0.5 + rng.gauss(0, 0.05),         # narrow band
            "fundamentals": 0.5 + rng.gauss(0, 0.07),
            "risk": min(1, max(0, 0.5 + rng.gauss(0, 0.15))),
        })
    return rows


def test_raw_signals_do_not_carry_their_weights_but_ranked_ones_do():
    raw = _cross_section()
    weights = {"momentum": 0.15, "portfolio": 0.15, "fundamentals": 0.15, "risk": 0.15}
    before = variance_shares(raw, weights=weights)
    assert before["momentum"] > 0.5  # momentum dominates raw

    ranked = rank_signals_within_lanes([("stock", "equity", s) for s in raw], min_lane_size=20)
    after = variance_shares(ranked, weights=weights)
    for key in weights:
        # equal weights -> each near a quarter, within a factor of two
        assert 0.125 <= after[key] <= 0.5, (key, after)
    assert abs(sum(after.values()) - 1.0) < 1e-3


def test_signal_with_fewer_than_five_distinct_values_is_left_out():
    rows = [{"momentum": i / 30, "ml_signal": 0.5 if i % 2 else 0.6} for i in range(30)]
    out = rank_signals_within_lanes([("stock", "equity", r) for r in rows], min_lane_size=20)
    assert all("ml_signal" not in o and "momentum" in o for o in out)


def test_small_lane_and_money_market_keep_raw_values():
    rows = [{"momentum": i / 10} for i in range(10)]
    out = rank_signals_within_lanes([("fund", "etf", r) for r in rows], min_lane_size=20)
    assert out == rows
    mm = [("stock", "money_market", {"momentum": 0.5, "risk": 0.5})]
    mm += [("stock", "equity", {"momentum": i / 40, "risk": i / 40}) for i in range(40)]
    out = rank_signals_within_lanes(mm, min_lane_size=20)
    assert out[0] == {"momentum": 0.5, "risk": 0.5}


def test_lanes_are_ranked_separately_and_ties_share_a_rank():
    entries = [("stock", "equity", {"a": float(i)}) for i in range(10)]
    entries += [("fund", "etf", {"a": float(i) * 100}) for i in range(10)]
    entries += [("stock", "equity", {"a": 5.0}) for _ in range(2)]  # ties with i=5
    out = rank_signals_within_lanes(entries, min_lane_size=5)
    assert out[0]["a"] < out[9]["a"]
    assert out[5]["a"] == out[20]["a"] == out[21]["a"]
    assert set(WEIGHTS)  # module import sanity


# --- WP12a: the outcome band beside each pick ----------------------------------------------


def test_conformal_band_takes_the_corrected_empirical_quantiles():
    from app.decision.discover.calibrator import conformal_band

    values = [float(i) for i in range(1, 21)]  # 20 dates: ranks floor(0.1*21)=2 and ceil(0.9*21)=19
    assert conformal_band(values) == (2.0, 19.0)
    assert conformal_band(values[:19]) is None  # under 20 dates: no band
    big = [float(i) for i in range(1, 101)]
    assert conformal_band(big) == (10.0, 91.0)  # floor(10.1)=10, ceil(90.9)=91


def test_outcome_band_averages_per_issue_date_and_needs_twenty_dates():
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from conftest import _memory_db

    from app.decision.discover.calibrator import outcome_band
    from app.foundation.models.entities import DiscoveryPrediction, User

    db = _memory_db()
    user = User(id=uuid4().hex, username="band", password_hash="x")
    db.add(user)
    now = datetime(2026, 11, 10, tzinfo=UTC)

    def add(days_ago, excess, run, itype="equity", symbol="A", status="resolved"):
        db.add(DiscoveryPrediction(
            user_id=user.id, run_id=run, symbol=symbol, predicted_at=now - timedelta(days=days_ago),
            horizon_days=21, resolve_at=now, direction="buy", conviction=0.6, outcome_status=status,
            excess_return=excess, features_json={"signal_breakdown": {"quant_signals": {"instrument_type": itype}}},
        ))

    for i in range(19):  # 19 issue dates, two picks each: the date's mean counts once
        add(40 + i, 0.01 * i, f"r{i}", symbol="A")
        add(40 + i, 0.01 * i, f"r{i}", symbol="B")
    db.commit()
    assert outcome_band(db, user.id, "stock", now) is None  # 19 dates
    add(80, 0.20, "r19")
    add(3, 0.9, "recent", status="resolved")   # younger than a horizon: ignored
    add(50, 0.5, "fundrun", itype="etf")       # another lane
    db.commit()
    band = outcome_band(db, user.id, "stock", now)
    assert band is not None and band["n_dates"] == 20 and band["lane"] == "stock"
    assert band["low"] == 0.01 and band["high"] == 0.18
    assert "against MSCI World" in band["text"] and "20 issue dates" in band["text"]
    assert outcome_band(db, user.id, "fund", now) is None
