"""Historical ingredient panels for the locked exam (ADR 0019 amendment 1)."""
import math

import numpy as np
import pandas as pd
import pytest

from app.lab.ranking_replay import panels as P


def _rows(n_months: int = 40, n_us: int = 4, n_ez: int = 3, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    months = pd.date_range("2016-01-31", periods=n_months, freq="ME")
    stocks = [("USA", f"u{i}", 1000 + i) for i in range(n_us)] + [("DEU", f"e{i}", None) for i in range(n_ez)]
    out = []
    for m in months:
        for k, (cty, gvkey, permno) in enumerate(stocks):
            out.append({
                "gvkey": gvkey, "permno": permno, "eom": m, "excntry": cty,
                "size_grp": "mega" if k % 2 == 0 else "large", "me": 100.0 + 10 * k,
                "mom_12_1": rng.normal(0.05, 0.2), "be_me": 0.5, "gp_at": 0.3, "at_gr1": 0.0,
                "ret_1_0": rng.normal(0.01, 0.05), "rvol_21d": 0.015,
                "ret_exc_lead1m": rng.normal(0.01, 0.05),
            })
    return pd.DataFrame(out)


def test_dedup_keeps_the_largest_listing():
    raw = _rows(2)
    dup = raw.iloc[[0]].assign(me=1.0)
    out = P.prepare_rows(pd.concat([raw, dup]))
    assert len(out) == len(raw)
    first = out[(out["entity"] == "u0:1000") & (out["eom"] == raw["eom"].iloc[0])]
    assert first["me"].iloc[0] == 100.0


def test_nullable_permno_reads_as_no_permno():
    raw = _rows(1).astype({"permno": "Int64"})
    assert set(P.prepare_rows(raw)["entity"]) >= {"u0:1000", "e0:"}


def test_universe_takes_top_n_per_region():
    df = P.prepare_rows(_rows(1, n_us=5, n_ez=5))
    members = P.universe(df, df["eom"].iloc[0], top_n=2)
    assert sorted(members["entity"]) == ["e3:", "e4:", "u3:1003", "u4:1004"]


def test_momentum_is_the_live_mid_rank():
    scores = P.momentum_scores(pd.Series([0.3, -0.1, 0.1, np.nan], index=list("abcd")))
    assert scores.to_dict() == pytest.approx({"a": 5 / 6, "b": 1 / 6, "c": 0.5})


def test_fundamentals_matches_the_live_heuristic():
    assert P.fundamentals_score(0.5, 0.3, 0.0) == 0.5
    assert P.fundamentals_score(1.0, 0.6, -0.2) == 0.95
    assert P.fundamentals_score(np.nan, np.nan, np.nan) is None


def test_risk_score_formula():
    flat = P.risk_score(np.full(12, 0.01), np.zeros(12))
    sigma = 0.01
    expected = 1 - (0.4 * sigma * math.sqrt(252) / 0.60 + 0.3 * sigma * P._CVAR95_PER_SIGMA / 0.06)
    assert flat == pytest.approx(expected)
    assert P.max_drawdown(np.array([0.1, -0.5, 0.2])) == pytest.approx(0.5)


def test_benchmark_and_portfolio_scores():
    rng = np.random.default_rng(1)
    bench = rng.normal(0.01, 0.04, 36)
    assert P.benchmark_score(bench + 0.01, bench + rng.normal(0, 1e-9, 36)) > 0.99
    assert P.benchmark_score(bench[:11], bench[:11]) is None
    assert P.portfolio_score(bench, bench) == pytest.approx(0.4)
    assert P.portfolio_score(bench[:5], bench[:5]) == 0.5


def test_composite_renormalises_over_present_ingredients():
    assert P.composite_score({"momentum": 1.0, "risk": None, "fundamentals": 0.0}) == pytest.approx(0.5)
    assert P.composite_score({"momentum": None}) is None


def test_panels_have_the_replay_shape_and_no_look_ahead():
    raw = _rows()
    periods = [pd.Timestamp("2019-01-31"), pd.Timestamp("2019-02-28")]
    out = P.build_panels(raw, periods, top_n=3)
    assert set(out) == {"momentum", "risk", "benchmark", "portfolio", "fundamentals", "composite"}
    for frame in out.values():
        assert list(frame.columns) == ["period", "entity", "score", "forward"]
        assert set(frame["period"]) == {"2019-01-31", "2019-02-28"}
    # Changing anything after 2019-01 must not move a 2019-01 score.
    later = raw["eom"] > pd.Timestamp("2019-01-31")
    shifted = raw.copy()
    for col in ("ret_1_0", "rvol_21d", "mom_12_1", "me"):
        shifted.loc[later, col] = shifted.loc[later, col] * 3
    again = P.build_panels(shifted, periods[:1], top_n=3)
    for name, frame in again.items():
        jan = out[name][out[name]["period"] == "2019-01-31"].reset_index(drop=True)
        pd.testing.assert_series_equal(frame["score"], jan["score"])


def test_forward_is_excess_over_the_region_benchmark():
    raw = _rows(40)
    jan = pd.Timestamp("2019-01-31")
    raw.loc[raw["eom"] == jan, "ret_exc_lead1m"] = 0.02
    out = P.build_panels(raw, [jan], top_n=3)
    assert np.allclose(out["momentum"]["forward"], 0.0)


def _bench_rows():
    m = pd.date_range("2020-01-31", periods=3, freq="ME")
    rows = []
    for i, eom in enumerate(m):
        rows.append({"gvkey": "a", "permno": 1, "eom": eom, "excntry": "USA", "me": 100.0,
                     "size_grp": "large" if i == 0 else "small", "ret_1_0": -0.5 if i == 1 else 0.0,
                     "ret_exc_lead1m": 0.0})
        rows.append({"gvkey": "b", "permno": 2, "eom": eom, "excntry": "USA", "me": 100.0,
                     "size_grp": "large", "ret_1_0": 0.1, "ret_exc_lead1m": 0.0})
    return P.prepare_rows(pd.DataFrame(rows).assign(mom_12_1=0.0, be_me=0.5, gp_at=0.3, at_gr1=0.0, rvol_21d=0.01))


def test_benchmark_keeps_a_member_that_left_the_size_group():
    monthly, _ = P.benchmark_returns(_bench_rows(), ("USA",))
    # Both were large at January's end, so February is (−0.5 + 0.1) / 2.
    assert monthly[pd.Timestamp("2020-02-29")] == pytest.approx(-0.2)
    # By February's end only b is large, so March is b alone.
    assert monthly[pd.Timestamp("2020-03-31")] == pytest.approx(0.1)


def test_benchmark_has_no_weights_after_a_missing_month():
    df = _bench_rows()
    df = df[df["eom"] != pd.Timestamp("2020-02-29")]
    monthly, _ = P.benchmark_returns(df, ("USA",))
    assert np.isnan(monthly[pd.Timestamp("2020-03-31")])


def test_total_loss_annualises_to_minus_one():
    assert P._annualised(np.array([-1.0] + [0.0] * 11)) == -1.0
    assert P.benchmark_score(np.array([-1.0] + [0.0] * 11), np.zeros(12)) < 0.5
