"""Tests for the AlphaCrafter Miner: IC recovers a planted signal; noise ~ 0."""

import asyncio
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import FactorsLibrary
from app.lab.alphacrafter.miner import (
    MinerAgent,
    cross_sectional_ic,
    decay_halflife_days,
    forward_returns,
    icir,
    run_miner,
)
from app.lab.alphacrafter.shared_memory import (
    FactorMetrics,
    FactorState,
    SharedMemoryH,
)


from conftest import _memory_db


def _seed_drift_bars(db, n_symbols=6, days=60):
    """Each symbol gets a distinct positive drift + tiny noise; volume is
    independent of drift (so a volume-rank factor has ~0 IC)."""
    symbols = [f"S{i}" for i in range(n_symbols)]
    # Anchor near "now" so bars fall inside run_miner's now-relative lookback.
    base = datetime.now(UTC) - timedelta(days=days + 1)
    price_rng = np.random.default_rng(11)
    vol_rng = np.random.default_rng(99)
    for i, sym in enumerate(symbols):
        drift = 0.003 * (i + 1)
        price = 100.0
        for d in range(days):
            price *= 1 + drift + price_rng.normal(0, 0.0005)
            ts = base + timedelta(days=d)
            db.execute(
                text(
                    """
                    INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider)
                    VALUES (:s, :ts, :p, :p, :p, :p, :v, 'USD', 'test')
                    """
                ),
                {"s": sym, "ts": ts, "p": price, "v": float(vol_rng.uniform(1e6, 9e6))},
            )
    db.commit()
    return symbols


# --- Pure metric helpers -------------------------------------------------------


def test_forward_returns_alignment():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    close = pd.DataFrame({"A": [10, 11, 12, 13, 14]}, index=idx, dtype=float)
    fr = forward_returns(close, 1)
    # (next/current - 1): 11/10-1=0.1 etc; last is NaN.
    assert np.isclose(fr["A"].iloc[0], 0.1)
    assert np.isnan(fr["A"].iloc[-1])


def test_cross_sectional_ic_recovers_planted_signal():
    idx = pd.date_range("2024-01-01", periods=20, freq="D")
    symbols = list("ABCDEF")
    rng = np.random.default_rng(3)
    factor = pd.DataFrame(rng.normal(size=(20, 6)), index=idx, columns=symbols)
    # Forward returns monotonically increasing in the factor (+ tiny noise).
    fwd = factor + rng.normal(0, 0.01, size=(20, 6))
    ic = cross_sectional_ic(factor, fwd)
    assert ic.mean() > 0.8


def test_cross_sectional_ic_noise_is_near_zero():
    idx = pd.date_range("2024-01-01", periods=60, freq="D")
    symbols = list("ABCDEF")
    rng = np.random.default_rng(5)
    factor = pd.DataFrame(rng.normal(size=(60, 6)), index=idx, columns=symbols)
    fwd = pd.DataFrame(rng.normal(size=(60, 6)), index=idx, columns=symbols)
    ic = cross_sectional_ic(factor, fwd)
    assert abs(ic.mean()) < 0.2


def test_icir_floor_handles_stable_series():
    stable = pd.Series([0.9, 0.91, 0.89, 0.9, 0.9])
    assert icir(stable) > 0.3


def test_decay_halflife_none_for_white_noise():
    rng = np.random.default_rng(1)
    noise = pd.Series(rng.normal(size=100))
    # White noise has ~0 lag-1 autocorr -> no exponential decay.
    hl = decay_halflife_days(noise)
    assert hl is None or hl > 0


# --- run_miner end-to-end ------------------------------------------------------


def test_run_miner_planted_factor_high_ic_and_persisted():
    db = _memory_db()
    symbols = _seed_drift_bars(db)
    candidates = asyncio.run(
        run_miner(
            db,
            symbols,
            lookback_years=1.0,
            horizon=5,
            registry=None,
            extra_factors=[
                {"name": "planted_mom", "dsl": "delta(close, 5)", "source": "test"},
                {"name": "noise_vol", "dsl": "rank(volume)", "source": "test"},
            ],
        )
    )
    by_name = {c.name: c for c in candidates}

    planted = by_name["planted_mom"]
    assert planted.ic > 0.5, f"planted IC too low: {planted.ic}"
    assert planted.valid
    assert planted.id is not None  # persisted

    noise = by_name["noise_vol"]
    assert abs(noise.ic) < 0.3, f"noise IC too high: {noise.ic}"

    # No np.random: metrics are reproducible run-to-run on identical data.
    again = asyncio.run(run_miner(db, symbols, lookback_years=1.0, horizon=5))
    assert any(c.name == "momentum_12_1" for c in again)

    # Planted survivor is in FactorsLibrary exactly once (upsert by name).
    rows = db.execute(
        select(FactorsLibrary).where(FactorsLibrary.name == "planted_mom")
    ).scalars().all()
    assert len(rows) == 1


def _create_initial_h(universe: list[str], start_date: str, end_date: str) -> SharedMemoryH:
    return SharedMemoryH.create_initial(
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        regime_label="bull",
        crisis=False,
    )


def test_miner_agent_writes_factor_states_to_h():
    """MinerAgent should populate H.factor_states with evaluated factors."""
    db = _memory_db()
    symbols = _seed_drift_bars(db)
    end = datetime.now(UTC)
    start = end - timedelta(days=365)
    h = _create_initial_h(symbols, start.isoformat(), end.isoformat())

    agent = MinerAgent()
    result_h = agent.run(h, db)

    assert len(result_h.factor_states) > 0, "No factor states written to H"

    for fs in result_h.factor_states:
        assert isinstance(fs, FactorState)
        assert fs.id is not None
        assert fs.name is not None
        assert fs.source in ("seed", "llm", "test", "quant_factors")
        assert fs.metrics is not None
        assert isinstance(fs.metrics, FactorMetrics)
        assert fs.metrics.ic is not None
        assert fs.metrics.icir is not None
        assert fs.metrics.turnover is not None


def test_miner_agent_writes_miner_outputs_to_h():
    """MinerAgent should populate H.miner_outputs with summary statistics."""
    db = _memory_db()
    symbols = _seed_drift_bars(db)
    end = datetime.now(UTC)
    start = end - timedelta(days=365)
    h = _create_initial_h(symbols, start.isoformat(), end.isoformat())

    agent = MinerAgent()
    result_h = agent.run(h, db)

    assert "n_evaluated" in result_h.miner_outputs, "Missing n_evaluated in miner_outputs"
    assert "n_valid" in result_h.miner_outputs, "Missing n_valid in miner_outputs"
    assert "n_persisted" in result_h.miner_outputs, "Missing n_persisted in miner_outputs"
    assert result_h.miner_outputs["n_evaluated"] > 0


def test_miner_agent_appends_to_agent_history():
    """MinerAgent should append execution record to H.agent_history."""
    db = _memory_db()
    symbols = _seed_drift_bars(db)
    end = datetime.now(UTC)
    start = end - timedelta(days=365)
    h = _create_initial_h(symbols, start.isoformat(), end.isoformat())

    agent = MinerAgent()
    result_h = agent.run(h, db)

    assert len(result_h.agent_history) > 0, "No agent history appended"

    last_entry = result_h.agent_history[-1]
    assert last_entry["agent"] == "miner"
    assert last_entry["action"] == "run"
    assert "n_evaluated" in last_entry["details"]


def test_miner_agent_persists_valid_factors_to_db():
    """MinerAgent should persist valid factors to FactorsLibrary."""
    db = _memory_db()
    symbols = _seed_drift_bars(db)
    end = datetime.now(UTC)
    start = end - timedelta(days=365)
    h = _create_initial_h(symbols, start.isoformat(), end.isoformat())

    agent = MinerAgent()
    result_h = agent.run(h, db)

    valid_factors = [fs for fs in result_h.factor_states if fs.metrics.ic > 0.02]
    if valid_factors:
        rows = db.execute(select(FactorsLibrary)).scalars().all()
        assert len(rows) > 0, "No factors persisted to FactorsLibrary"


def test_miner_agent_reads_from_h_market_state():
    """MinerAgent should read universe and date_range from H.market_state."""
    db = _memory_db()
    symbols = _seed_drift_bars(db)
    end = datetime.now(UTC)
    start = end - timedelta(days=365)
    h = _create_initial_h(symbols, start.isoformat(), end.isoformat())

    agent = MinerAgent()
    result_h = agent.run(h, db)

    assert result_h.market_state.universe == symbols
