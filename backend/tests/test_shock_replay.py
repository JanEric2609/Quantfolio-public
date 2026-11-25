"""Confirmed shock-day event study (research only): detection, confirmation, continue vs fade, the frozen grid."""
import math
from datetime import date, timedelta

from app.foundation import technical
from app.lab import shock_replay as sr

INST = {"spread_bp": 1.0, "usd": False, "overnight_long": 0.000083, "overnight_short": 0.000139}


def _calm(n=60, start=100.0):
    out, d, px = [], date(2020, 1, 1), start
    for i in range(n):
        px *= 1 + (0.001 if i % 2 else -0.001)
        out.append(technical.Bar(d, px, px * 1.002, px * 0.998, px))
        d += timedelta(days=1)
    return out


def _shock(bars, up=True, close_pos=0.95, size=0.05):
    last = bars[-1]
    o = last.close
    c = o * (1 + size if up else 1 - size)
    lo, hi = min(o, c) * 0.999, max(o, c) * 1.001
    close = lo + close_pos * (hi - lo) if up else hi - (1 - close_pos) * (hi - lo)
    return bars + [technical.Bar(last.date + timedelta(days=1), o, hi, lo, close)]


def _follow(bars, drift, n=8):
    out = list(bars)
    for _ in range(n):
        p = out[-1]
        o = p.close
        c = o * (1 + drift)
        out.append(technical.Bar(p.date + timedelta(days=1), o, max(o, c) * 1.001, min(o, c) * 0.999, c))
    return out


def test_a_confirmed_big_up_day_is_a_shock_and_an_unconfirmed_one_is_not():
    hit = sr.shocks(_follow(_shock(_calm()), 0.0))
    assert len(hit) == 1 and hit[0].sign == 1 and hit[0].sigmas >= 3
    miss = sr.shocks(_follow(_shock(_calm(), close_pos=0.4), 0.0))
    assert miss == []  # the day reversed away from its high: no confirmation


def test_continue_wins_and_fade_loses_when_the_move_runs_on():
    bars = _follow(_shock(_calm()), 0.01)
    s = sr.shocks(bars)[0]
    cont = sr.trade_net_bp(bars, s, "continue", 5, INST)
    fade = sr.trade_net_bp(bars, s, "fade", 5, INST)
    assert cont and fade and cont[1] > 0 > fade[1]


def test_down_shock_continue_is_short_and_data_ending_early_gives_none():
    bars = _follow(_shock(_calm(), up=False), -0.01, n=8)
    s = sr.shocks(bars)[0]
    assert s.sign == -1 and sr.trade_net_bp(bars, s, "continue", 5, INST)[1] > 0
    assert sr.trade_net_bp(bars, s, "continue", 50, INST) is None


def test_the_grid_is_frozen_and_run_reports_every_cell_with_holm():
    assert len(sr.CELLS) == 8 and sr.CELLS[0] == "k2_continue_h1" and sr.CELLS[-1] == "k3_fade_h5"
    res = sr.run({"DAX": _follow(_shock(_calm()), 0.01), "SPX": _follow(_shock(_calm(), up=False), -0.01)},
                 {"DAX": INST, "SPX": INST})
    assert set(res) == set(sr.CELLS)
    for cell in sr.CELLS:
        assert res[cell]["p_holm"] <= 1.0 and "t" not in res[cell] and "t_days" in res[cell]
    assert res["k3_continue_h5"]["events"] == 2 and not math.isnan(res["k3_continue_h5"]["mean_net_bp"])


def test_the_exit_bar_is_pinned_for_both_horizons():
    # shock on day index s; entry at the open of s+1; exit at the close of s+1+H; one night held per day of H
    bars = _follow(_shock(_calm()), 0.01, n=8)
    s = sr.shocks(bars)[0]
    for h in sr.HORIZONS:
        entry, exit_ = bars[s.i + 1].open, bars[s.i + 1 + h].close
        gross = (exit_ / entry - 1) * 1e4
        cost = 1.0 + INST["overnight_long"] * h * 1e4
        got = sr.trade_net_bp(bars, s, "continue", h, INST)
        assert got and got[0] == bars[s.i + 1].date and abs(got[1] - (gross - cost)) < 1e-6
    assert sr.trade_net_bp(_follow(_shock(_calm()), 0.01, n=3), sr.shocks(_follow(_shock(_calm()), 0.01, n=3))[0], "continue", 5, INST) is None
