"""swing_trend replay (research only): trend entries, gap-through-stop fills, the flipped control."""
from datetime import date, timedelta

from app.foundation import technical
from app.lab import swing_replay

INST = {"spread_bp": 1.0, "usd": False, "overnight_long": 0.000083, "overnight_short": 0.000139}


def _bars(closes, gap_at=None, gap_to=None):
    out, d = [], date(2020, 1, 1)
    for i, c in enumerate(closes):
        o = gap_to if gap_at == i else c
        out.append(technical.Bar(d, o, max(o, c) * 1.002, min(o, c) * 0.998, c))
        d += timedelta(days=1)
    return out


def test_steady_uptrend_goes_long_and_wins():
    bars = _bars([100 + 0.5 * i for i in range(200)])
    trades, gaps = swing_replay.simulate("DAX", bars, INST)
    assert trades and {t.direction for t in trades} == {"long"}
    assert sum(t.net for t in trades) > 0 and gaps["gap_overruns"] == 0


def test_flipped_control_trades_the_opposite_side_and_loses_in_a_trend():
    bars = _bars([100 + 0.5 * i for i in range(200)])
    trades, _ = swing_replay.simulate("DAX", bars, INST, flipped=True)
    assert trades and {t.direction for t in trades} == {"short"}
    assert sum(t.net for t in trades) < 0


def test_gap_through_the_stop_fills_at_the_open_and_is_counted():
    closes = [100 + 0.5 * i for i in range(120)] + [150.0] * 30
    bars = _bars(closes, gap_at=100, gap_to=80.0)  # a 20 % gap down while long
    trades, gaps = swing_replay.simulate("DAX", bars, INST)
    assert gaps["gap_overruns"] >= 1
    assert any(t.reason == "stop" and t.net < -5.0 for t in trades)  # lost more than the 5 EUR risk


def test_run_reports_cell_control_and_gaps():
    res = swing_replay.run({"DAX": _bars([100 + 0.5 * i for i in range(200)])}, {"DAX": INST})
    assert set(res) == {"swing_trend", "flipped_control"} and "gaps" in res["swing_trend"]
