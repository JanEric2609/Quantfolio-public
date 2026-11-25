"""The commodity/FX wrapper: cost scaling, family isolation and a no-edge noise run through the shared engine."""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app.foundation import technical
from app.lab import commodity_fx_replay as cf


def _bars(n: int, seed: int) -> list[technical.Bar]:
    rng = random.Random(seed)
    px, d, out = 100.0, date(2010, 1, 4), []
    for _ in range(n):
        o = px
        px *= 1 + rng.gauss(0, 0.01) + (0.06 if rng.random() < 0.01 else 0.0) * rng.choice((-1, 1))
        out.append(technical.Bar(date=d, open=o, high=max(o, px) * 1.002, low=min(o, px) * 0.998, close=px))
        d += timedelta(days=1)
    return out


def test_stress_scales_costs_and_copies():
    s = cf.stress(cf.FX, 2.0)
    assert s["EURUSD"]["spread_bp"] == pytest.approx(cf.FX["EURUSD"]["spread_bp"] * 2)
    assert s["EURUSD"]["overnight_long"] == pytest.approx(cf.FX["EURUSD"]["overnight_long"] * 2)
    assert cf.FX["EURUSD"]["spread_bp"] == 0.9  # original untouched
    with pytest.raises(ValueError):
        cf.stress(cf.FX, 0)


def test_tables_have_the_fields_the_engine_reads():
    for table in cf.FAMILIES.values():
        for spec in table.values():
            assert {"ticker", "spread_bp", "usd", "overnight_long", "overnight_short", "vol"} <= set(spec)
            assert spec["spread_bp"] > 0


def test_run_family_skips_missing_instruments_and_is_null_on_noise():
    bars = {"GOLD": _bars(1500, 1), "WTI": _bars(1500, 2)}
    res = cf.run_family(cf.COMMODITIES, bars, draws=20)
    assert res["trials"] == 4  # ewma trigger only: 2 directions x 2 exits
    assert set(res["legs"]) == {"GOLD", "WTI"}
    assert all(c["verdict"] == "null" for c in res["cells"].values())
