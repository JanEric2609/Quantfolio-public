from datetime import date, timedelta
from unittest.mock import patch

from app.foundation import technical as t
from app.lab import signal_replay as sr

INST = {"spread_bp": 2.0, "usd": False, "kind": "index", "overnight_long": 0.0001, "overnight_short": 0.0001}


def _bars(closes):
    s = date(2020, 1, 1)
    return [t.Bar(s + timedelta(days=i), c, c * 1.002, c * 0.998, c) for i, c in enumerate(closes)]


def test_nights_count_over_a_weekend():
    assert sr.nights(date(2026, 11, 20), date(2026, 11, 23)) == 3
    assert sr.nights(date(2026, 11, 23), date(2026, 11, 24)) == 1


def test_trend_series_makes_money_for_trend_and_loses_for_band():
    # alternating flat/ramps: crosses fire, then trend continues; mean-reversion fades the continuation.
    closes = [100.0] * 220
    for k in range(6):
        closes += [closes[-1] * (1.01 ** j) for j in range(1, 60)]
        closes += [closes[-1] * (0.99 ** j) for j in range(1, 60)]
    bars = _bars(closes)
    trend = sr.simulate("X", bars, INST, cells=("ta_trend:cfd",))
    band = sr.simulate("X", bars, INST, cells=("ta_band:cfd",))
    assert trend and band
    assert sum(x.net for x in trend) > 0
    assert sum(x.net for x in band) < sum(x.net for x in trend)


def test_costs_are_applied():
    closes = [100.0] * 220
    for k in range(4):
        closes += [closes[-1] * (1.01 ** j) for j in range(1, 60)]
        closes += [closes[-1] * (0.99 ** j) for j in range(1, 60)]
    bars = _bars(closes)
    cheap = sr.simulate("X", bars, {**INST, "spread_bp": 0.0, "overnight_long": 0, "overnight_short": 0},
                        cells=("ta_trend:cfd",))
    dear = sr.simulate("X", bars, {**INST, "spread_bp": 50.0}, cells=("ta_trend:cfd",))
    assert sum(x.net for x in dear) < sum(x.net for x in cheap)
    usd = sr.simulate("X", bars, {**INST, "usd": True, "spread_bp": 0.0, "overnight_long": 0, "overnight_short": 0},
                      cells=("ta_trend:cfd",))
    assert sum(x.net for x in usd) < sum(x.net for x in cheap) or all(x.net <= 0 for x in cheap)


def test_run_pooled_and_stress():
    closes = [100.0] * 220 + [100.0 * (1.01 ** j) for j in range(1, 80)]
    out = sr.run({"X": _bars(closes)}, {"X": {**INST, "kind": "stock"}}, stock_spread_bp=15.0)
    assert set(out) == set(sr.CELLS) and "pooled" in out["ta_trend:cfd"]
    assert sr.summarize([])["n"] == 0


def test_record_trials_once_per_cell():
    with patch.object(sr, "record_trial") as rec:
        assert sr.record_trials(None, "ab" * 32) == 3
    assert rec.call_count == 3
