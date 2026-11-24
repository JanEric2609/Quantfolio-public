from datetime import date, timedelta

from app.decision.desk.event_rules import with_provisional
from app.foundation import technical as t


def _bars(closes, start=date(2025, 1, 1)):
    return [t.Bar(start + timedelta(days=i), c, c + 0.5, c - 0.5, c) for i, c in enumerate(closes)]


def test_flat_data_gives_no_signal():
    bars = _bars([100.0] * 300)
    assert t.rule_hits(bars) == []
    assert t.rsi([100.0] * 10, 2) == 50.0


def test_short_history_has_no_rsi2_signal():
    closes = [100 + i for i in range(100)] + [90.0, 80.0]
    assert [h for h in t.rule_hits(_bars(closes)) if h.cell == "ta_rsi2:cfd"] == []


def test_rsi2_long_above_200_day():
    closes = [100 + i * 0.5 for i in range(250)] + [200.0, 190.0]
    hits = {h.cell: h for h in t.rule_hits(_bars(closes))}
    assert hits["ta_rsi2:cfd"].direction == "long" and hits["ta_rsi2:cfd"].stop_atr == 1.5


def test_rsi2_short_below_200_day():
    closes = [300 - i * 0.5 for i in range(250)] + [185.5, 195.5]
    hits = {h.cell: h for h in t.rule_hits(_bars(closes))}
    assert hits["ta_rsi2:cfd"].direction == "short"


def test_bollinger_fade_both_ways():
    low = {h.cell: h for h in t.rule_hits(_bars([100.0] * 19 + [90.0]))}
    assert low["ta_band:cfd"].direction == "long"
    high = {h.cell: h for h in t.rule_hits(_bars([100.0] * 19 + [110.0]))}
    assert high["ta_band:cfd"].direction == "short"
    assert t.exit_hit("ta_band:cfd", "long", _bars([100.0] * 19 + [100.0])) == "middle_band"


def test_ema_cross_on_the_last_bar_only():
    base = [100.0 - i * 0.2 for i in range(80)]
    seen = None
    for step in range(1, 40):
        closes = base + [base[-1] + 1.5 * k for k in range(1, step + 1)]
        hits = [h for h in t.rule_hits(_bars(closes)) if h.cell == "ta_trend:cfd"]
        if hits:
            seen = (step, hits[0])
            break
    assert seen is not None and seen[1].direction == "long"
    step = seen[0]
    prior = base + [base[-1] + 1.5 * k for k in range(1, step)]
    assert [h for h in t.rule_hits(_bars(prior)) if h.cell == "ta_trend:cfd"] == []
    # and the opposite cross exits the long
    down = base + [base[-1] + 1.5 * k for k in range(1, 30)]
    while t.exit_hit("ta_trend:cfd", "long", _bars(down)) is None and len(down) < 400:
        down.append(down[-1] - 3.0)
    assert t.exit_hit("ta_trend:cfd", "long", _bars(down)) == "opposite_cross"


def test_atr_and_indicators():
    bars = _bars([100.0 + i for i in range(30)])
    assert abs(t.atr(bars, 14) - 1.5) < 1e-9
    assert t.atr(bars[:10], 14) is None
    lo, mid, hi = t.bollinger([1.0, 2.0, 3.0, 4.0], 4, 2.0)
    assert mid == 2.5 and lo < mid < hi
    assert t.ema_series([1.0, 2.0], 5) == []


def test_provisional_bar():
    bars = _bars([100.0, 101.0])
    today = bars[-1].date + timedelta(days=1)
    assert with_provisional(bars, 105.0, today)[-1].close == 105.0
    replaced = with_provisional(bars, 105.0, bars[-1].date)
    assert len(replaced) == 2 and replaced[-1].close == 105.0
