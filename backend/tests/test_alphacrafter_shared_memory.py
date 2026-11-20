"""Tests for AlphaCrafter SharedMemoryH coordination state."""

from datetime import datetime

from app.lab.alphacrafter.shared_memory import (
    FactorMetrics,
    FactorState,
    MarketState,
    SharedMemoryH,
)


# ---------------------------------------------------------------------------
# MarketState
# ---------------------------------------------------------------------------

class TestMarketState:
    def test_creation(self):
        ms = MarketState(
            universe=["AAPL", "MSFT", "GOOGL"],
            date_range=("2024-01-01", "2024-12-31"),
            regime_label="bull",
            crisis=False,
            macro_indicators={"us10y": 4.2, "vix": 15.3},
            data_availability={"AAPL": 252, "MSFT": 240},
        )
        assert ms.universe == ["AAPL", "MSFT", "GOOGL"]
        assert ms.date_range == ("2024-01-01", "2024-12-31")
        assert ms.regime_label == "bull"
        assert ms.crisis is False
        assert ms.macro_indicators == {"us10y": 4.2, "vix": 15.3}
        assert ms.data_availability == {"AAPL": 252, "MSFT": 240}


# ---------------------------------------------------------------------------
# FactorMetrics
# ---------------------------------------------------------------------------

class TestFactorMetrics:
    def test_creation(self):
        fm = FactorMetrics(
            ic=0.05,
            icir=1.2,
            turnover=0.3,
            decay_halflife_days=21.0,
            n_obs=500,
        )
        assert fm.ic == 0.05
        assert fm.icir == 1.2
        assert fm.turnover == 0.3
        assert fm.decay_halflife_days == 21.0
        assert fm.n_obs == 500

    def test_creation_no_decay(self):
        fm = FactorMetrics(ic=0.03, icir=0.8, turnover=0.5, decay_halflife_days=None, n_obs=200)
        assert fm.decay_halflife_days is None


# ---------------------------------------------------------------------------
# FactorState
# ---------------------------------------------------------------------------

class TestFactorState:
    def test_creation(self):
        fs = FactorState(
            id="f1",
            name="momentum_12_1",
            source="llm",
            category="momentum",
            formula="close / close.shift(252) - 1",
            dsl=None,
            metrics=FactorMetrics(ic=0.04, icir=1.0, turnover=0.25, decay_halflife_days=30.0, n_obs=400),
            regime_applicability={"bull": 0.8, "bear": 0.3},
        )
        assert fs.id == "f1"
        assert fs.name == "momentum_12_1"
        assert fs.source == "llm"
        assert fs.category == "momentum"
        assert fs.formula == "close / close.shift(252) - 1"
        assert fs.dsl is None
        assert fs.metrics.ic == 0.04
        assert fs.regime_applicability == {"bull": 0.8, "bear": 0.3}


# ---------------------------------------------------------------------------
# SharedMemoryH — creation
# ---------------------------------------------------------------------------

class TestSharedMemoryCreation:
    def test_empty_defaults(self):
        ms = MarketState(
            universe=[], date_range=("2024-01-01", "2024-12-31"),
            regime_label=None, crisis=False, macro_indicators={}, data_availability={},
        )
        h = SharedMemoryH(market_state=ms)
        assert h.factor_states == []
        assert h.regime_assessment == {}
        assert h.miner_outputs == {}
        assert h.screener_outputs == {}
        assert h.trader_outputs == {}
        assert h.evaluation == {}
        assert h.agent_history == []
        assert isinstance(h.created_at, datetime)
        assert isinstance(h.updated_at, datetime)


# ---------------------------------------------------------------------------
# append_history
# ---------------------------------------------------------------------------

class TestAppendHistory:
    def test_append_history(self):
        ms = MarketState(
            universe=[], date_range=("2024-01-01", "2024-12-31"),
            regime_label=None, crisis=False, macro_indicators={}, data_availability={},
        )
        h = SharedMemoryH(market_state=ms)
        h.append_history("miner", "propose_factors", {"count": 3})
        assert len(h.agent_history) == 1
        entry = h.agent_history[0]
        assert entry["agent"] == "miner"
        assert entry["action"] == "propose_factors"
        assert entry["details"] == {"count": 3}
        assert "timestamp" in entry
        # Verify timestamp is parseable
        ts = datetime.fromisoformat(entry["timestamp"])
        assert ts.tzinfo is not None

    def test_append_multiple(self):
        ms = MarketState(
            universe=[], date_range=("2024-01-01", "2024-12-31"),
            regime_label=None, crisis=False, macro_indicators={}, data_availability={},
        )
        h = SharedMemoryH(market_state=ms)
        h.append_history("miner", "start", {})
        h.append_history("screener", "evaluate", {"passed": 5})
        h.append_history("trader", "rebalance", {})
        assert len(h.agent_history) == 3
        assert [e["agent"] for e in h.agent_history] == ["miner", "screener", "trader"]


# ---------------------------------------------------------------------------
# to_dict / from_dict serialization round-trip
# ---------------------------------------------------------------------------

class TestSerialization:
    def _make_h(self) -> SharedMemoryH:
        ms = MarketState(
            universe=["AAPL", "MSFT"],
            date_range=("2024-01-01", "2024-12-31"),
            regime_label="bull",
            crisis=False,
            macro_indicators={"us10y": 4.2},
            data_availability={"AAPL": 252},
        )
        fs = FactorState(
            id="f1",
            name="momentum_12_1",
            source="seed",
            category="momentum",
            formula="close / close.shift(252) - 1",
            dsl="momentum(close, 252)",
            metrics=FactorMetrics(ic=0.05, icir=1.2, turnover=0.3, decay_halflife_days=21.0, n_obs=500),
            regime_applicability={"bull": 0.9},
        )
        h = SharedMemoryH(
            market_state=ms,
            factor_states=[fs],
            regime_assessment={"label": "bull", "confidence": 0.85},
            miner_outputs={"proposed": 3},
            screener_outputs={"passed": 2},
            trader_outputs={"weights": {"AAPL": 0.6, "MSFT": 0.4}},
            evaluation={"sharpe": 1.5},
        )
        h.append_history("miner", "start", {})
        return h

    def test_to_dict_roundtrip(self):
        h = self._make_h()
        d = h.to_dict()
        assert isinstance(d, dict)
        h2 = SharedMemoryH.from_dict(d)
        assert h2.market_state.universe == ["AAPL", "MSFT"]
        assert h2.market_state.regime_label == "bull"
        assert len(h2.factor_states) == 1
        assert h2.factor_states[0].id == "f1"
        assert h2.factor_states[0].metrics.ic == 0.05
        assert h2.factor_states[0].metrics.decay_halflife_days == 21.0
        assert h2.regime_assessment == {"label": "bull", "confidence": 0.85}
        assert h2.miner_outputs == {"proposed": 3}
        assert h2.screener_outputs == {"passed": 2}
        assert h2.trader_outputs == {"weights": {"AAPL": 0.6, "MSFT": 0.4}}
        assert h2.evaluation == {"sharpe": 1.5}
        assert len(h2.agent_history) == 1
        assert h2.agent_history[0]["agent"] == "miner"

    def test_to_dict_keys(self):
        h = self._make_h()
        d = h.to_dict()
        expected_keys = {
            "market_state", "factor_states", "regime_assessment",
            "miner_outputs", "screener_outputs", "trader_outputs",
            "evaluation", "agent_history", "created_at", "updated_at",
        }
        assert expected_keys == set(d.keys())


# ---------------------------------------------------------------------------
# create_initial factory
# ---------------------------------------------------------------------------

class TestCreateInitial:
    def test_create_initial(self):
        h = SharedMemoryH.create_initial(
            universe=["AAPL", "MSFT", "GOOGL"],
            start_date="2024-01-01",
            end_date="2024-12-31",
        )
        assert h.market_state.universe == ["AAPL", "MSFT", "GOOGL"]
        assert h.market_state.date_range == ("2024-01-01", "2024-12-31")
        assert h.market_state.regime_label is None
        assert h.market_state.crisis is False
        assert h.market_state.macro_indicators == {}
        assert h.factor_states == []
        assert h.agent_history == []

    def test_create_initial_with_optionals(self):
        h = SharedMemoryH.create_initial(
            universe=["SPY"],
            start_date="2023-01-01",
            end_date="2023-12-31",
            regime_label="bear",
            crisis=True,
            macro_indicators={"us10y": 5.0},
        )
        assert h.market_state.regime_label == "bear"
        assert h.market_state.crisis is True
        assert h.market_state.macro_indicators == {"us10y": 5.0}
