"""Slow-trend cell and replay: rules on closes, no clock exit, and a noise run that must stay null."""
from __future__ import annotations

import random
from datetime import date, timedelta

from app.foundation import technical
from app.lab import trend_replay as tr
from app.lab.algo_lab import trend_cell as tc

INST = {"spread_bp": 2.0, "usd": False, "overnight_long": 0.0001, "overnight_short": 0.0001}


def _bars(closes: list[float]) -> list[technical.Bar]:
    d, out, prev = date(2005, 1, 3), [], closes[0]
    for c in closes:
        out.append(technical.Bar(date=d, open=prev, high=max(prev, c) * 1.003, low=min(prev, c) * 0.997, close=c))
        prev, d = c, d + timedelta(days=1)
    return out


def _walk(n: int, seed: int, drift: float = 0.0) -> list[float]:
    rng, px, out = random.Random(seed), 100.0, []
    for _ in range(n):
        px *= 1 + rng.gauss(drift, 0.01)
        out.append(px)
    return out


def test_breakout_signals_need_a_full_window_and_follow_the_side():
    assert tc.breakout_signals([100.0] * 100) == {}
    up = [100.0] * 100 + [101.0, 101.5]
    assert tc.breakout_signals(up) == {100: "long"}
    down = [100.0] * 100 + [99.0, 98.5]
    assert tc.breakout_signals(down) == {100: "short"}


def test_ema_exit_and_next_action():
    closes = [100.0 + i for i in range(120)]
    assert tc.ema_exit(closes, "long") is False
    assert tc.ema_exit(closes + [50.0], "long") is True
    assert tc.ema_exit(closes + [500.0], "short") is True
    assert tc.next_action(closes, "long") == "hold"
    assert tc.next_action(closes + [50.0], "long") == "exit"
    assert tc.next_action([100.0] * 120, None) == "wait"
    assert tc.next_action([100.0] * 110 + [100.0 + i for i in range(1, 11)], None) == "enter_long"


def test_trend_replay_has_no_time_exit_and_noise_is_null():
    fam = {"a": _bars(_walk(1800, 1)), "b": _bars(_walk(1800, 2))}
    res = tr.run({"noise": (fam, {"a": INST, "b": INST})}, draws=20)
    assert res["trials"] == 2 and all(c["verdict"] == "null" for c in res["cells"].values())
    for c in res["cells"].values():
        assert c["side_placebo"] is not None
        assert set(c["gaps"]) == {"stops", "gap_overruns", "data_end"}


def test_a_persistent_trend_earns_in_the_replay_before_any_verdict_rule():
    closes = _walk(1500, 3, drift=0.002)
    res = tr.run({"up": ({"a": _bars(closes)}, {"a": INST})}, draws=10)
    pooled = res["cells"]["up:chandelier"]["pooled"]
    assert pooled["n"] >= 1 and pooled["mean_eur"] > 0


def test_ema_exit_follows_the_position_and_is_armed_by_a_favourable_close():
    # Prices fall from 200 to 100 (EMA50 stays above), then rise slowly. A long signalled while the close is below the EMA
    # must not be stopped out on its fill bar: the exit is armed only after a close on the favourable side.
    closes = [200.0] * 60 + [100.0] * 60 + [101.0 + 0.2 * i for i in range(30)]
    bars = _bars(closes)
    sig_i = len(closes) - 30
    long_dir, _ = tr._simulate("a", bars, INST, {sig_i: "long"}, "ema_cross", 5.0, "t")
    assert long_dir and long_dir[0].hold_days > 2
    # A short on the same day (close below EMA, favourable) is armed at once and exits when the close rises back through.
    short_dir, _ = tr._simulate("a", bars, INST, {sig_i: "short"}, "ema_cross", 5.0, "t")
    assert short_dir and short_dir[0].direction == "short"
    # Mirror: a short signalled above its EMA is not stopped out on the fill bar either (symmetric placebo exposure).
    up = [100.0] * 60 + [200.0] * 60 + [199.0 - 0.2 * i for i in range(30)]
    s2, _ = tr._simulate("a", _bars(up), INST, {len(up) - 30: "short"}, "ema_cross", 5.0, "t")
    assert s2 and s2[0].hold_days > 2
