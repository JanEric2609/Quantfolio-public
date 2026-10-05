"""The satellite simulation (report Phase 3, step 3) and the engine modes it uses."""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering import _pit_duckdb
from app.foundation.models.entities import TrialLedgerEntry
from app.lab.pooled_model import fit as fit_mod
from app.lab.pooled_model import run_model_study
from app.lab.quant_lab import CostModel, LabBacktestSpec, run_lab_backtest
from app.lab.satellite import core_tax_annual, parse_monthly_rf, pick, run_satellite_study
from app.lab.satellite import rf as rf_mod
from app.lab.satellite import study as study_mod
from app.lab.satellite.picks import holding_months
from app.lab.satellite.spec import STOCK_TAX_RATE


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture(autouse=True)
def _duckdb(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_MEMORY_LIMIT", "256MB")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_THREADS", "1")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_TMPDIR", str(tmp_path / "duckdb"))
    monkeypatch.setattr(fit_mod, "GBM_THREADS", 1)
    _pit_duckdb.reset_connection()
    yield
    _pit_duckdb.reset_connection()


# --- risk-free rate ---------------------------------------------------------------

_FF_TEXT = """This file was created using the 202607 CRSP database.
The 1-month TBill rate data until 202405 are from Ibbotson Associates.

,Mkt-RF,SMB,HML,RF
192607,   2.89,  -2.42,  -2.75,   0.22
192608,   2.64,  -1.44,   4.13,   0.25

 Annual Factors: January-December
,Mkt-RF,SMB,HML,RF
  1927,   29.47,  -2.04,  -3.54,   3.12
"""


def test_rf_parser_reads_the_monthly_block_only():
    rf = parse_monthly_rf(_FF_TEXT)
    assert list(rf.index.astype(str)) == ["1926-07", "1926-08"]
    assert rf.tolist() == pytest.approx([0.0022, 0.0025])


def test_rf_is_cached_and_a_stale_cache_survives_a_failed_download(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rf_mod, "_download", lambda: calls.append(1) or parse_monthly_rf(_FF_TEXT))
    first = rf_mod.load_risk_free(tmp_path)
    second = rf_mod.load_risk_free(tmp_path)
    assert len(calls) == 1 and second.equals(first)

    cache = tmp_path / rf_mod.CACHE_NAME
    old = time.time() - 60 * 86_400
    os.utime(cache, (old, old))

    def fail():
        raise OSError("offline")

    monkeypatch.setattr(rf_mod, "_download", fail)
    assert rf_mod.load_risk_free(tmp_path).equals(first)
    cache.unlink()
    with pytest.raises(RuntimeError, match="risk-free"):
        rf_mod.load_risk_free(tmp_path)


# --- pick rules -------------------------------------------------------------------


def _rows(month_scores: dict[str, dict[str, float]], size: str = "mega") -> pd.DataFrame:
    return pd.DataFrame([
        {"eom": pd.Timestamp(m), "gvkey": g, "size_grp": size, "s": v}
        for m, scores in month_scores.items() for g, v in scores.items()
    ])


def test_picks_hold_while_above_the_bar_and_fill_from_the_top():
    months = [pd.Timestamp("2000-03-31"), pd.Timestamp("2000-06-30"), pd.Timestamp("2000-09-30")]
    rows = _rows({
        "2000-03-31": {"a": 0.9, "b": 0.8, "c": 0.7, "d": 0.1},
        "2000-06-30": {"a": 0.2, "b": 0.8, "c": 0.9, "d": 0.95},
        # Ties break by gvkey; "c" left the data and is sold.
        "2000-09-30": {"a": 0.5, "b": 0.8, "d": 0.95, "e": 0.5},
    })
    bars = pd.Series([0.5, 0.5, 0.5], index=months)

    reviews = pick(rows, "s", bars, months, positions=3)

    assert [(r.sells, r.buys) for r in reviews] == [
        ((), ("a", "b", "c")),
        (("a",), ("d",)),
        (("c",), ("a",)),
    ]
    assert holding_months(reviews, [months[0], pd.Timestamp("2000-04-30"), pd.Timestamp("2000-05-31"),
                                    months[1], pd.Timestamp("2000-07-31"), pd.Timestamp("2000-08-31"),
                                    months[2]]) == [3, 6, 7, 4, 1]


def test_picks_buy_only_eligible_size_groups_but_keep_a_holding_that_shrank():
    months = [pd.Timestamp("2000-03-31"), pd.Timestamp("2000-06-30")]
    rows = pd.concat([
        _rows({"2000-03-31": {"a": 0.9}, "2000-06-30": {"b": 0.8}}),
        _rows({"2000-03-31": {"z": 0.99}, "2000-06-30": {"a": 0.9, "z": 0.99}}, size="large"),
    ])
    reviews = pick(rows, "s", pd.Series([0.5, 0.5], index=months), months, positions=2)
    assert [(r.sells, r.buys) for r in reviews] == [((), ("a",)), ((), ("b",))]


# --- engine modes -----------------------------------------------------------------


def _engine(prices: dict[str, list[float]], weights: dict[str, list[float]], **spec):
    dates = pd.date_range("2000-01-31", periods=len(next(iter(prices.values()))), freq="ME")
    result = run_lab_backtest(
        LabBacktestSpec(symbols=list(prices), initial_cash=100.0, allowance_total_eur=Decimal("0"), **spec),
        pd.DataFrame(prices, index=dates), pd.DataFrame(weights, index=dates),
    )
    return result


def test_trades_mode_holds_unnamed_symbols_and_spends_the_cash_on_buys():
    nan = np.nan
    result = _engine(
        {"a": [1.0, 2.0, 2.0], "b": [1.0, 1.0, 1.0], "c": [1.0, 1.0, 4.0]},
        {"a": [1.0, nan, nan], "b": [1.0, 0.0, nan], "c": [nan, 1.0, nan]},
        cost_model=CostModel(commission_bps=0.0, spread_bps=0.0, apply_tax_drag=False),
        rebalance="trades",
    )
    # a doubles and is never trimmed; b's 50 moves into c, which quadruples.
    assert result.equity_curve.tolist() == pytest.approx([100.0, 150.0, 300.0])
    assert result.trades == 4


def test_losses_carried_forward_offset_later_gains():
    nan = np.nan
    prices = {"a": [1.0, 0.5, 0.5, 0.5], "b": [1.0, 1.0, 1.0, 2.0]}
    weights = {"a": [1.0, 0.0, nan, nan], "b": [nan, 1.0, nan, 0.0]}
    costs = CostModel(commission_bps=0.0, spread_bps=0.0, apply_tax_drag=True)

    plain = _engine(prices, weights, cost_model=costs, rebalance="trades")
    offset = _engine(prices, weights, cost_model=costs, rebalance="trades", offset_losses=True)

    # a loses 50; b doubles the remaining 50 into a 50 gain. Without the
    # carry-forward the full gain is taxed; with it nothing is.
    assert plain.total_tax_drag_eur == pytest.approx(50 * STOCK_TAX_RATE, abs=0.02)
    assert offset.total_tax_drag_eur == 0.0


# --- core tax ---------------------------------------------------------------------


def test_core_tax_is_deferred_and_partially_exempt():
    assert core_tax_annual(0.0) == 0.0
    drag = core_tax_annual(0.08)
    # Cheaper than paying the full stock rate every year, but not free.
    assert 0.0 < drag < 0.08 * STOCK_TAX_RATE * 0.7


# --- the study --------------------------------------------------------------------


def _panel(root: Path, months: int = 130, stocks: int = 30) -> Path:
    """Two countries of mega caps; next-month return driven by gp_at."""
    rng = np.random.default_rng(0)
    rows = []
    for eom in pd.date_range("2000-01-31", periods=months, freq="ME"):
        for country in ("USA", "DEU"):
            gp = rng.normal(size=stocks)
            for i in range(stocks):
                rows.append({
                    "gvkey": f"{country}{i}", "permno": None, "eom": eom, "excntry": country, "size_grp": "mega",
                    "me": 100.0 + i, "be_me": rng.normal(), "mom_12_1": rng.normal(), "gp_at": gp[i],
                    "ret_exc_lead1m": 0.02 * gp[i] + 0.005 * rng.normal(),
                    "source": "wrds_factor_characteristics", "ingested_at": pd.Timestamp("2026-09-01"),
                })
    out = root / "wrds_factor_characteristics_pit"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(out / "x.0000.parquet", index=False)
    return root


@pytest.fixture
def scored(tmp_path, monkeypatch):
    rf = pd.Series(0.002, index=pd.period_range("1990-01", "2020-12", freq="M"), name="rf")
    monkeypatch.setattr(study_mod, "load_risk_free", lambda cache_dir: rf)
    panel = _panel(tmp_path)
    assert run_model_study(_memory_db(), panel_dir=panel, n_folds=5) is not None
    return panel


def test_the_model_satellite_beats_the_core_and_costs_and_tax_are_charged(scored):
    study = run_satellite_study(_memory_db(), panel_dir=scored, persist=False)

    assert study is not None
    cards = {(c.broker, c.model): c for c in study.cards}
    ridge, baseline = cards[("dkb", "ridge")], cards[("dkb", "value_momentum")]
    # gp_at drives returns: the model's picks earn it, value + momentum's do not.
    assert ridge.gross_excess_annual > 0.15
    assert ridge.vs_baseline_annual > 0.1
    assert abs(baseline.gross_excess_annual) < abs(ridge.gross_excess_annual)
    # Every step down the chain costs something.
    assert ridge.gross_excess_annual > ridge.net_excess_annual
    assert ridge.satellite_tax_annual > 0 and ridge.core_tax_annual > 0
    assert ridge.trades_per_year > 0 and ridge.holding_months >= 3
    assert 0.5 < ridge.p_beat_core <= 1.0
    # Reviews are quarter ends only.
    assert {pd.Timestamp(t["eom"]).month for t in study.trades["dkb:ridge"]} <= {3, 6, 9, 12}
    # The cheaper broker holds more stocks and pays less per trade.
    scalable = cards[("scalable", "ridge")]
    assert max(len(t["buys"]) for t in study.trades["scalable:ridge"]) == 10
    assert scalable.gross_excess_annual - scalable.net_excess_annual < ridge.gross_excess_annual - ridge.net_excess_annual


def test_persisted_run_records_each_model_once_and_writes_results(scored):
    db = _memory_db()
    study = run_satellite_study(db, panel_dir=scored)
    run_satellite_study(db, panel_dir=scored)

    keys = sorted(e.trial_key for e in db.query(TrialLedgerEntry).filter_by(context="satellite"))
    assert keys == ["world:dkb:gbm", "world:dkb:ridge", "world:scalable:gbm", "world:scalable:ridge"]
    assert study is not None and study.artifact is not None
    saved = json.loads(study.artifact.read_text())
    assert {(c["broker"], c["model"]) for c in saved["cards"]} == {
        (b, m) for b in ("dkb", "scalable") for m in ("ridge", "gbm", "value_momentum")
    }
    assert [b["positions"] for b in saved["spec"]["brokers"]] == [3, 10] and saved["trades"]["dkb:ridge"]


def test_no_scores_means_no_study(tmp_path):
    assert run_satellite_study(_memory_db(), panel_dir=tmp_path, persist=False) is None
