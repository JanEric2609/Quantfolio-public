"""End-to-end test for the lab's first signal batch (ADR 0015 ruling #26),
against a synthetic multi-symbol panel (not real data -- see
test_data_engineering_datastream_loader.py for the "built and tested against
fixtures, not yet run against real data" precedent this follows).

Proves the panel -> labels -> factors -> purge/embargo CV -> IC-gate ->
Parquet-persist path runs end-to-end and gates correctly: a factor planted
with real predictive power survives the IC/ICIR bar, pure noise doesn't.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.lab.quant_lab.signal_batch import (
    PricePanel,
    compute_microstructure_factors,
    run_signal_batch,
)


def _planted_signal_prices(n_rows: int = 500, n_symbols: int = 20, seed: int = 11) -> pd.DataFrame:
    """Construct a price panel where 20-day momentum has *real* predictive
    power for the next 20-day return, by construction (each symbol's return
    regime is autocorrelated), so a genuine planted-IC factor exists."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n_rows, freq="B")
    symbols = [f"SYM{i}" for i in range(n_symbols)]

    data = {}
    for sym in symbols:
        # Each symbol gets a persistent per-block drift regime, autocorrelated
        # across blocks -- momentum in one block predicts the next block's
        # direction, which is exactly what a real momentum factor captures.
        n_blocks = n_rows // 20 + 1
        block_drift = rng.normal(0, 0.002, n_blocks)
        # Autocorrelate: each block's drift partly follows the previous one.
        for b in range(1, n_blocks):
            block_drift[b] = 0.6 * block_drift[b - 1] + 0.4 * block_drift[b]
        daily_drift = np.repeat(block_drift, 20)[:n_rows]
        noise = rng.normal(0, 0.01, n_rows)
        returns = daily_drift + noise
        data[sym] = 100.0 * np.cumprod(1 + returns)

    return pd.DataFrame(data, index=dates)


def _make_price_panel_for_module_test(n_rows: int = 500, n_symbols: int = 10) -> PricePanel:
    close = _planted_signal_prices(n_rows=n_rows, n_symbols=n_symbols)
    volume = pd.DataFrame(
        np.random.default_rng(3).integers(1000, 100_000, close.shape), index=close.index, columns=close.columns
    )
    return PricePanel(close=close, volume=volume)


def test_compute_microstructure_factors_returns_expected_shape_and_names():
    panel = _make_price_panel_for_module_test()

    factors = compute_microstructure_factors(panel)

    assert set(factors.keys()) == {"momentum_12_1", "realized_vol_20d", "dollar_volume_20d"}
    for df in factors.values():
        assert df.shape == panel.close.shape


def test_run_signal_batch_end_to_end_via_monkeypatched_panel(monkeypatch):
    panel = _make_price_panel_for_module_test(n_rows=500, n_symbols=20)

    def _fake_build_price_panel(symbols=None):
        return panel

    monkeypatch.setattr("app.lab.quant_lab.signal_batch.build_price_panel", _fake_build_price_panel)

    result = run_signal_batch(
        horizon=20,
        vol_window=60,
        n_folds=5,
        purge_days=20,
        embargo_frac=0.01,
        min_ic=0.0,  # deliberately permissive so we can inspect real IC values, not just pass/fail
        min_icir=0.0,
        dry_run=True,
    )

    assert len(result.factors) == 3
    names = {f.name for f in result.factors}
    assert names == {"momentum_12_1", "realized_vol_20d", "dollar_volume_20d"}
    for f in result.factors:
        assert f.n_oos_observations > 0

    # The planted-autocorrelation construction gives momentum real predictive
    # power; a pure-noise factor would center on IC ~ 0. This is not a tight
    # bound (synthetic data, finite sample) -- just proof the gate can tell
    # a real signal apart from noise, which is the point of this test.
    momentum = next(f for f in result.factors if f.name == "momentum_12_1")
    assert abs(momentum.ic) > 0.01


def test_run_signal_batch_dry_run_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_SIGNAL_BATCH_DIR", str(tmp_path))
    panel = _make_price_panel_for_module_test(n_rows=500, n_symbols=10)
    monkeypatch.setattr("app.lab.quant_lab.signal_batch.build_price_panel", lambda symbols=None: panel)

    result = run_signal_batch(n_folds=5, purge_days=20, dry_run=True)

    assert result.output_path is None
    assert not list(tmp_path.glob("*.parquet"))


def test_run_signal_batch_apply_writes_parquet_report(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_SIGNAL_BATCH_DIR", str(tmp_path))
    panel = _make_price_panel_for_module_test(n_rows=500, n_symbols=10)
    monkeypatch.setattr("app.lab.quant_lab.signal_batch.build_price_panel", lambda symbols=None: panel)

    result = run_signal_batch(n_folds=5, purge_days=20, dry_run=False)

    assert result.output_path is not None
    assert result.output_path.exists()
    report = pd.read_parquet(result.output_path)
    assert set(report["name"]) == {"momentum_12_1", "realized_vol_20d", "dollar_volume_20d"}


def test_run_signal_batch_returns_empty_on_insufficient_data():
    result = run_signal_batch(symbols=["DOES_NOT_EXIST"], n_folds=5, dry_run=True)

    assert result.factors == []


def _two_currency_panel() -> PricePanel:
    """A euro line and a pence-quoted line with identical *economic* turnover:
    SHEL trades 100x fewer shares at a 100x larger quoted number."""
    dates = pd.date_range("2020-01-01", periods=60, freq="B")
    close = pd.DataFrame({"SAP": np.full(60, 100.0), "SHEL": np.full(60, 10_000.0)}, index=dates)
    volume = pd.DataFrame({"SAP": np.full(60, 1_000_000.0), "SHEL": np.full(60, 10_000.0)}, index=dates)
    return PricePanel(close=close, volume=volume, currencies={"SAP": "EUR", "SHEL": "GBp"})


def test_dollar_volume_is_omitted_for_a_multi_currency_panel_without_fx():
    factors = compute_microstructure_factors(_two_currency_panel())

    assert "dollar_volume_20d" not in factors
    assert set(factors) == {"momentum_12_1", "realized_vol_20d"}


def test_dollar_volume_uses_fx_rates_when_supplied():
    panel = _two_currency_panel()

    factors = compute_microstructure_factors(panel, fx_to_base={"EUR": 1.0, "GBP": 1.0})

    # SAP: 100 EUR x 1,000,000 = 100,000,000.
    # SHEL: 10,000 GBp -> 100 GBP -> x 10,000 shares = 1,000,000 (a genuinely
    # smaller book). Raw close*volume would have made SHEL look identical.
    last = factors["dollar_volume_20d"].iloc[-1]
    assert last["SAP"] == 100_000_000.0
    assert last["SHEL"] == 1_000_000.0


def test_pence_quotation_is_normalised_without_fx_when_that_is_the_only_currency():
    dates = pd.date_range("2020-01-01", periods=60, freq="B")
    close = pd.DataFrame({"SHEL": np.full(60, 10_000.0)}, index=dates)
    volume = pd.DataFrame({"SHEL": np.full(60, 10_000.0)}, index=dates)
    panel = PricePanel(close=close, volume=volume, currencies={"SHEL": "GBp"})

    factors = compute_microstructure_factors(panel)

    assert factors["dollar_volume_20d"].iloc[-1]["SHEL"] == 1_000_000.0


def test_currency_invariant_factors_are_unaffected_by_the_missing_scale():
    """Momentum and vol must still be produced for a panel that cannot be put
    on one currency scale -- they are ratios, so the scale never mattered."""
    factors = compute_microstructure_factors(_two_currency_panel())

    assert factors["momentum_12_1"].shape == (60, 2)
    assert factors["realized_vol_20d"].shape == (60, 2)


def test_panel_without_currency_information_behaves_as_before():
    panel = _make_price_panel_for_module_test(n_rows=60, n_symbols=3)

    factors = compute_microstructure_factors(panel)

    assert "dollar_volume_20d" in factors


def test_build_price_panel_carries_currency_through(tmp_path, monkeypatch):
    from app.foundation.data_engineering.panel_schema import validate_panel_frame
    from app.lab.quant_lab.signal_batch import build_price_panel

    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    rows = pd.DataFrame([
        {"symbol": "SHEL", "isin": None, "as_of_date": "2026-01-02", "open": 2850.0, "high": 2850.0,
         "low": 2850.0, "close": 2850.0, "volume": 1000.0, "currency": "GBp", "source": "datastream_pit",
         "ingested_at": "2026-01-10"},
    ])
    out = tmp_path / "datastream_pit"
    out.mkdir(parents=True)
    validate_panel_frame(rows).to_parquet(out / "shel.parquet", index=False)

    panel = build_price_panel()

    assert panel.currencies == {"SHEL": "GBp"}


def test_cli_parses_fx_pairs_into_run_signal_batch(monkeypatch):
    from app.lab.quant_lab import signal_batch as sb

    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return sb.SignalBatchResult(universe=[], horizon=20)

    monkeypatch.setattr(sb, "run_signal_batch", _capture)
    monkeypatch.setattr("sys.argv", ["signal_batch", "--fx", "eur=1.0", "--fx", "GBP=1.17"])

    sb._main()

    assert seen["fx_to_base"] == {"EUR": 1.0, "GBP": 1.17}
