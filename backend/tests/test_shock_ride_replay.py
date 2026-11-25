"""Strong-day entries with state exits and no time stop (research only): triggers, exits, placebo, verdict."""
import random
from datetime import date, timedelta

from app.foundation import technical
from app.lab import shock_ride_replay as sr

INST = {"spread_bp": 1.0, "usd": False, "overnight_long": 0.000083, "overnight_short": 0.000139}


def _bars(closes, opens=None):
    out, d = [], date(2015, 1, 1)
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        out.append(technical.Bar(d, o, max(o, c) * 1.002, min(o, c) * 0.998, c))
        d += timedelta(days=1)
    return out


def _quiet_then_shock(drift_after: float, n_after: int = 60, shock: float = -0.06):
    rng = random.Random(1)
    c = [100.0]
    for _ in range(120):
        c.append(c[-1] * (1 + rng.gauss(0, 0.004)))
    c.append(c[-1] * (1 + shock))  # the strong day, index 121
    for _ in range(n_after):
        c.append(c[-1] * (1 + drift_after + rng.gauss(0, 0.001)))
    return c


def test_strong_day_trigger_fires_on_the_shock_and_respects_direction():
    bars = _bars(_quiet_then_shock(-0.004))
    assert sr.strong_signals(bars, "with")[121] == "short"
    assert sr.strong_signals(bars, "fade")[121] == "long"


def test_continuation_after_the_shock_wins_for_with_and_loses_for_fade_and_has_no_time_exit():
    bars = _bars(_quiet_then_shock(-0.004))
    win, _ = sr.simulate_signals("DAX", bars, INST, sr.strong_signals(bars, "with"), "chandelier")
    lose, _ = sr.simulate_signals("DAX", bars, INST, sr.strong_signals(bars, "fade"), "chandelier")
    assert win and sum(t.net for t in win) > 0 and win[0].direction == "short"
    assert lose and sum(t.net for t in lose) < 0
    assert max(t.hold_days for t in win) > 10  # the ride is not cut by a clock


def test_open_position_at_the_end_is_closed_as_data_end_and_counted():
    bars = _bars(_quiet_then_shock(-0.004, n_after=40))
    tr, gaps = sr.simulate_signals("DAX", bars, INST, sr.strong_signals(bars, "with"), "chandelier")
    assert tr[-1].reason == "data_end" and gaps["data_end"] == 1


def test_ema_cross_exit_fires_when_price_comes_back_through_the_average():
    closes = _quiet_then_shock(-0.004, n_after=10)
    closes += [closes[-1] * 1.04] * 5  # a sharp bounce back above the 10-day average
    bars = _bars(closes)
    tr, _ = sr.simulate_signals("DAX", bars, INST, sr.strong_signals(bars, "with"), "ema_cross")
    assert tr and tr[0].reason in {"ema_cross", "stop"}


def test_established_flag_needs_a_trend_in_force():
    bars = _bars(_quiet_then_shock(-0.004))
    assert sr.established(bars, 121) is False  # a fresh shock out of a flat market
    down = _bars([100 - 0.3 * i for i in range(130)] + [58.0])
    assert sr.established(down, 130) is True


def test_leg_stats_splits_overnight_and_intraday_signed_by_the_strong_day():
    opens = [100.0] * 60 + [100.0] * 5
    closes = [100.0] * 60 + [100.0, 106.0, 106.0, 106.0, 106.0]
    bars = _bars(closes, opens)
    st = sr.leg_stats(bars, {60: "long", 61: "long", 62: "long"})
    assert st["overnight_after_strong_signed"]["n"] == 3 and st["intraday_all_days"]["n"] > 0


def test_holm_is_monotone_and_capped():
    adj = sr.holm({"a": 0.01, "b": 0.02, "c": 0.9})
    assert adj["a"] <= adj["b"] <= adj["c"] <= 1.0 and abs(adj["a"] - 0.03) < 1e-9


def test_run_on_noise_is_null_with_a_placebo_and_declared_trials():
    rng = random.Random(7)
    c = [100.0]
    for _ in range(900):
        c.append(c[-1] * (1 + rng.gauss(0, 0.01)))
    res = sr.run({"DAX": _bars(c)}, {"DAX": INST}, draws=12)
    assert res["trials"] == 4
    for cell in res["cells"].values():
        assert cell["verdict"] == "null" and cell["placebo"] is not None
