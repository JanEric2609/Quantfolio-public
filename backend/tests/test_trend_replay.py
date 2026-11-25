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


def test_real_exit_is_the_declared_state_exit_and_only_the_control_is_armed():
    # Prices fall from 200 to 100 (EMA50 stays above), then rise slowly. A long signalled below its EMA is, by the declared
    # rule, an exit state: the real run leaves on the fill bar; the control (arm_exit) holds until a favourable close.
    closes = [200.0] * 60 + [100.0] * 60 + [101.0 + 0.2 * i for i in range(30)]
    bars = _bars(closes)
    sig_i = len(closes) - 30
    real, _ = tr._simulate("a", bars, INST, {sig_i: "long"}, "ema_cross", 5.0, "t")
    assert real and real[0].hold_days <= 2
    ctrl, _ = tr._simulate("a", bars, INST, {sig_i: "long"}, "ema_cross", 5.0, "t", arm_exit=True)
    assert ctrl and ctrl[0].hold_days > 2
    short, _ = tr._simulate("a", bars, INST, {sig_i: "short"}, "ema_cross", 5.0, "t")
    assert short and short[0].direction == "short"


def test_pure_drift_without_breakout_information_is_not_a_pass():
    # i.i.d. noise with a positive drift: the real trades are mostly long, and so must the control be.
    rng = random.Random(11)
    out = {}
    for k in ("a", "b", "c"):
        px, closes = 100.0, []
        for _ in range(4000):
            px *= 1 + 0.0005 + rng.gauss(0, 0.005)
            closes.append(px)
        out[k] = _bars(closes)
    res = tr.run({"x": (out, {k: INST for k in out})}, draws=30)
    assert all(c["verdict"] == "null" for c in res["cells"].values())
