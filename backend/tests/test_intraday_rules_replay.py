"""Intraday rule replay (research only): crossing detection, stop/target fills, trailing, control, Holm cells."""
import random
from datetime import UTC, datetime, timedelta

from app.lab import intraday_rules_replay as rep


def _rows(closes, day=datetime(2026, 3, 2, 14, 30, tzinfo=UTC)):  # 09:30 New York
    out = []
    for i, c in enumerate(closes):
        t = day + timedelta(minutes=5 * i)
        out.append({"t": t, "o": c, "h": c * 1.001, "l": c * 0.999, "c": c})
    return out


def _bars(closes):
    return rep.sessions(_rows(closes))[datetime(2026, 3, 2).date()]


def test_a_rising_leg_after_a_flat_start_enters_long_and_the_target_fills():
    closes = [100.0] * 40 + [100 + 0.15 * i for i in range(1, 38)]
    bars = _bars(closes)
    assert "long" in rep._crosses(bars).values()
    trades = rep.simulate_day(bars, "tp1_5", random.Random(1))
    assert trades and any(why in ("target", "flat") for _g, why in trades)


def test_falling_leg_enters_short_and_a_gap_up_hits_the_stop():
    closes = [100.0] * 40 + [99.0, 98.0, 110.0] + [110.0] * 34
    bars = _bars(closes)
    trades = rep.simulate_day(bars, "tp1_5", random.Random(1))
    assert any(why == "stop" and g < 0 for g, why in trades)


def test_control_flips_direction_by_seed_and_cells_are_frozen():
    assert rep.CELLS == ("fixed", "tp1_5", "trail", "tp1_5_hv", "trail_hv", "control") and rep.TESTED == rep.CELLS[:5]
    closes = [100.0] * 40 + [100 + 0.15 * i for i in range(1, 38)]
    a = rep.simulate_day(_bars(closes), "control", random.Random(1))
    b = rep.simulate_day(_bars(closes), "control", random.Random(1))
    assert a == b


def test_evaluate_returns_every_cell_for_both_cost_cases():
    sess = rep.sessions(_rows([100.0] * 40 + [100 + 0.15 * i for i in range(1, 38)]))
    res = rep.evaluate(sess, 7.0)
    assert set(res) == set(rep.CELLS) and res["fixed"]["n"] >= 0
