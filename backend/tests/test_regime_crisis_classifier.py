"""Tests for crisis gate and regime classifier orchestrator.

Tests cover:
- Pure crisis gate rule evaluation at threshold boundaries
- Classifier integration: features, model loading, crisis evaluation, persistence
"""

import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import MacroIndicator
from app.lab.regime.crisis_gate import evaluate_crisis
from app.lab.regime.classifier import classify_and_store
from app.lab.regime.features import build_regime_features
from app.lab.regime.hmm_model import RegimeHMM

@pytest.fixture(autouse=True)
def _no_persisted_jump_model(monkeypatch):
    """The live regime model is the jump model; these tests inject an HMM,
    so a jump artefact on the machine must not be picked up instead."""
    def _missing(model_id, base=None):
        raise ValueError(f"no artefact {model_id}")

    monkeypatch.setattr("app.lab.regime.classifier.JumpRegimeModel.load", _missing)



def _memory_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    # Create hypertables manually (SQLite doesn't support TimescaleDB)
    with engine.begin() as conn:
        # Create regime_snapshots table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP NOT NULL,
                label TEXT NOT NULL,
                score REAL NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}'
            )
        """))

        # Create bar_prices table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bar_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                ts TIMESTAMP NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                currency TEXT DEFAULT 'USD',
                provider TEXT NOT NULL
            )
        """))

    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def db():
    """Provide an in-memory test database."""
    return _memory_db()


# ============================================================================
# Tests for evaluate_crisis (pure function)
# ============================================================================

class TestEvaluateCrisis:
    """Test crisis gate rule evaluation."""

    def test_crisis_vix_above_threshold(self):
        """VIX strictly above threshold fires crisis."""
        result = evaluate_crisis(
            vix=31.0,
            credit_spread=2.0,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is True
        assert "vix" in result["triggers"]
        assert "credit_spread" not in result["triggers"]
        assert "drawdown" not in result["triggers"]

    def test_crisis_vix_at_threshold_no_fire(self):
        """VIX exactly equal to threshold does NOT fire."""
        result = evaluate_crisis(
            vix=30.0,
            credit_spread=2.0,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert "vix" not in result["triggers"]

    def test_crisis_vix_below_threshold(self):
        """VIX below threshold does not fire."""
        result = evaluate_crisis(
            vix=29.0,
            credit_spread=2.0,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert "vix" not in result["triggers"]

    def test_crisis_vix_none_never_fires(self):
        """VIX=None means that signal is unavailable and cannot trigger."""
        result = evaluate_crisis(
            vix=None,
            credit_spread=2.0,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert "vix" not in result["triggers"]

    def test_crisis_credit_spread_above_threshold(self):
        """Credit spread strictly above threshold fires crisis."""
        result = evaluate_crisis(
            vix=20.0,
            credit_spread=3.1,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is True
        assert "credit_spread" in result["triggers"]
        assert "vix" not in result["triggers"]

    def test_crisis_credit_spread_at_threshold_no_fire(self):
        """Credit spread exactly equal to threshold does NOT fire."""
        result = evaluate_crisis(
            vix=20.0,
            credit_spread=3.0,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert "credit_spread" not in result["triggers"]

    def test_crisis_drawdown_below_threshold(self):
        """Drawdown strictly less than (more negative than) threshold fires crisis."""
        result = evaluate_crisis(
            vix=20.0,
            credit_spread=2.0,
            drawdown=-0.16,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is True
        assert "drawdown" in result["triggers"]
        assert "vix" not in result["triggers"]

    def test_crisis_drawdown_at_threshold_no_fire(self):
        """Drawdown exactly equal to threshold does NOT fire."""
        result = evaluate_crisis(
            vix=20.0,
            credit_spread=2.0,
            drawdown=-0.15,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert "drawdown" not in result["triggers"]

    def test_crisis_drawdown_above_threshold_no_fire(self):
        """Drawdown less negative (higher) than threshold does not fire."""
        result = evaluate_crisis(
            vix=20.0,
            credit_spread=2.0,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert "drawdown" not in result["triggers"]

    def test_crisis_drawdown_none_never_fires(self):
        """Drawdown=None means that signal is unavailable and cannot trigger."""
        result = evaluate_crisis(
            vix=20.0,
            credit_spread=2.0,
            drawdown=None,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert "drawdown" not in result["triggers"]

    def test_crisis_multiple_triggers(self):
        """Multiple signals firing populate triggers list."""
        result = evaluate_crisis(
            vix=31.0,
            credit_spread=3.1,
            drawdown=-0.16,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is True
        assert set(result["triggers"]) == {"vix", "credit_spread", "drawdown"}

    def test_crisis_all_clear(self):
        """No signals fire → crisis=False, triggers=[]."""
        result = evaluate_crisis(
            vix=20.0,
            credit_spread=2.0,
            drawdown=-0.10,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert result["crisis"] is False
        assert result["triggers"] == []

    def test_crisis_result_structure(self):
        """Result always has 'crisis' and 'triggers' keys."""
        result = evaluate_crisis(
            vix=None,
            credit_spread=None,
            drawdown=None,
            vix_threshold=30.0,
            credit_spread_threshold=3.0,
            drawdown_threshold=-0.15,
        )
        assert "crisis" in result
        assert "triggers" in result
        assert isinstance(result["crisis"], bool)
        assert isinstance(result["triggers"], list)


# ============================================================================
# Tests for classify_and_store (integration)
# ============================================================================

class TestClassifyAndStore:
    """Test regime classifier orchestrator."""

    def _make_synthetic_bars(self, symbol: str, days: int = 420) -> pd.DataFrame:
        """Create synthetic ascending OHLCV bars for classifier test."""
        dates = pd.date_range("2020-01-01", periods=days, freq="D")
        # Ascending close prices with small random walk
        returns = np.random.randn(days) * 0.01
        close = 100 * np.exp(np.cumsum(returns))

        return pd.DataFrame({
            "symbol": symbol,
            "ts": dates,
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": 1000000,
            "currency": "USD",
            "provider": "test",
        })

    def test_classifier_writes_snapshot_with_model(self, db, monkeypatch):
        """Classifier writes a snapshot when bars and model are available."""
        # Insert synthetic bars
        bars_df = self._make_synthetic_bars("^STOXX50E", days=420)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()  # Convert Timestamp to datetime
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()

        # Build features and fit a model
        features = build_regime_features(
            bars_df[["ts", "close"]],
            macro_df=None,
            vol_window=21
        )
        model = RegimeHMM(n_init=1)
        model.fit(features)

        # Monkeypatch RegimeHMM.load to return the fitted model
        def mock_load(model_id, base=None):
            return model

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            mock_load,
        )

        # Use the timestamp from the last bar so lookback reaches the data
        now = bars_df["ts"].iloc[-1].to_pydatetime()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Run classifier
        result = classify_and_store(db, now=now, lookback_days=400)

        # Assert result structure
        assert "written" in result
        assert result["written"] is True
        assert "label" in result
        assert result["label"] in ("bull", "sideways", "bear")
        assert "score" in result
        assert 0 <= result["score"] <= 1
        assert "crisis" in result
        assert isinstance(result["crisis"], bool)

        # Assert snapshot was written to DB
        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 1

        # Assert snapshot content
        row = db.execute(text(
            "SELECT label, score, source FROM regime_snapshots"
        )).fetchone()
        assert row[0] == result["label"]
        assert abs(float(row[1]) - result["score"]) < 0.01
        assert row[2] == "hmm"

        # The daily call is also frozen in the trust ledger, one row per day.
        from app.foundation.models.entities import RegimeLabelHistory

        history = db.query(RegimeLabelHistory).all()
        assert len(history) == 1
        assert history[0].as_of == now.astimezone(timezone.utc).date()
        assert history[0].label == result["label"]
        assert history[0].model == "hmm"
        assert set(history[0].probabilities_json) == set(result["probs"])

    def test_classifier_returns_false_when_no_bars(self, db, monkeypatch):
        """Classifier returns written=False when bar_prices is empty."""
        # Monkeypatch to avoid actual model load
        def mock_load(model_id, base=None):
            raise Exception("Should not be called")

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            mock_load,
        )

        # Run classifier with empty DB (with a specific now to avoid timezone issues)
        now = datetime(2020, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = classify_and_store(db, now=now, lookback_days=400)

        # Assert returns written=False
        assert result["written"] is False

        # Assert nothing was written to DB
        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 0

    def test_classifier_returns_false_when_model_missing(self, db, monkeypatch):
        """Classifier returns written=False when RegimeHMM.load fails."""
        # Insert synthetic bars
        bars_df = self._make_synthetic_bars("^STOXX50E", days=420)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()  # Convert Timestamp to datetime
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()

        # Monkeypatch RegimeHMM.load to raise
        def mock_load(model_id, base=None):
            raise FileNotFoundError("Model not found")

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            mock_load,
        )

        # Use the timestamp from the last bar
        now = bars_df["ts"].iloc[-1].to_pydatetime()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Run classifier
        result = classify_and_store(db, now=now, lookback_days=400)

        # Assert returns written=False with reason
        assert result["written"] is False
        assert "reason" in result
        assert "model_unavailable" in result["reason"]

        # Assert nothing was written to DB
        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 0

    def test_classifier_uses_macro_data(self, db, monkeypatch):
        """Classifier includes macro data when available."""
        # Insert synthetic bars
        bars_df = self._make_synthetic_bars("^STOXX50E", days=420)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()  # Convert Timestamp to datetime
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()

        # Insert macro indicators every other day through the newest bar (a
        # series that ends early would leave only stale rows to classify,
        # which classify_and_store now refuses).
        base_date = bars_df["ts"].min()
        for i in range(215):
            current_date = base_date + timedelta(days=i*2)
            if current_date <= bars_df["ts"].max():
                db.add(MacroIndicator(
                    name="VIXCLS",
                    value=Decimal(str(20.0 + i * 0.1)),
                    date=current_date.date(),
                    source="fred",
                ))
        db.commit()

        # Build features and fit a model
        features = build_regime_features(
            bars_df[["ts", "close"]],
            macro_df=None,
            vol_window=21
        )
        model = RegimeHMM(n_init=1)
        model.fit(features)

        # Monkeypatch RegimeHMM.load to return the fitted model
        def mock_load(model_id, base=None):
            return model

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            mock_load,
        )

        # Use the timestamp from the last bar
        now = bars_df["ts"].iloc[-1].to_pydatetime()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Run classifier
        result = classify_and_store(db, now=now, lookback_days=400)

        # Assert result has payload with crisis info
        assert result["written"] is True
        assert "probs" in result
        assert result["feature_ts"] == now

    def test_classifier_refuses_a_row_older_than_the_bars(self, db, monkeypatch):
        """A macro series that stopped early leaves only old complete rows; the
        classifier used to label that old row as today's regime (prod,
        2026-09-21..28) and must refuse instead."""
        bars_df = self._make_synthetic_bars("^STOXX50E", days=420)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()
        base_date = bars_df["ts"].min()
        for i in range(100):  # every other day, ending ~220 days before the newest bar
            db.add(MacroIndicator(
                name="VIXCLS", value=Decimal("20"), date=(base_date + timedelta(days=i * 2)).date(), source="fred",
            ))
        db.commit()
        model = RegimeHMM(n_init=1)
        model.fit(build_regime_features(bars_df[["ts", "close"]], macro_df=None, vol_window=21))
        monkeypatch.setattr("app.lab.regime.classifier.RegimeHMM.load", lambda model_id, base=None: model)
        now = bars_df["ts"].iloc[-1].to_pydatetime()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        result = classify_and_store(db, now=now, lookback_days=400)

        assert result["written"] is False
        assert result["reason"] == "stale_features"

    def _seed(self, db, days: int = 420) -> pd.DataFrame:
        bars_df = self._make_synthetic_bars("^STOXX50E", days=days)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()
        return bars_df

    def test_classifier_uses_the_jump_model_and_records_its_source(self, db, monkeypatch):
        """ADR 0004 amendment: the jump model is the live regime model."""
        from app.lab.regime.jump_model import JumpRegimeModel

        bars_df = self._seed(db)
        model = JumpRegimeModel(n_init=2).fit(
            build_regime_features(bars_df[["ts", "close"]], macro_df=None, vol_window=21)
        )
        monkeypatch.setattr("app.lab.regime.classifier.JumpRegimeModel.load", lambda model_id, base=None: model)
        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            lambda model_id, base=None: (_ for _ in ()).throw(AssertionError("HMM not expected")),
        )
        now = bars_df["ts"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)

        result = classify_and_store(db, now=now, lookback_days=400)

        assert result["written"] is True
        assert result["source"] == "jump"
        assert result["label"] in {"bull", "sideways", "bear"}
        stored = db.execute(text("SELECT source FROM regime_snapshots ORDER BY ts DESC LIMIT 1")).scalar()
        assert stored == "jump"

    def test_classifier_falls_back_to_the_hmm_without_a_jump_model(self, db, monkeypatch):
        bars_df = self._seed(db)
        model = RegimeHMM(n_init=1)
        model.fit(build_regime_features(bars_df[["ts", "close"]], macro_df=None, vol_window=21))
        monkeypatch.setattr("app.lab.regime.classifier.RegimeHMM.load", lambda model_id, base=None: model)
        now = bars_df["ts"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)

        result = classify_and_store(db, now=now, lookback_days=400)

        assert result["written"] is True
        assert result["source"] == "hmm"

    def test_classifier_payload_includes_crisis_details(self, db, monkeypatch):
        """Classifier payload includes crisis and triggers information."""
        # Insert synthetic bars
        bars_df = self._make_synthetic_bars("^STOXX50E", days=420)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()  # Convert Timestamp to datetime
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()

        # Build features and fit a model
        features = build_regime_features(
            bars_df[["ts", "close"]],
            macro_df=None,
            vol_window=21
        )
        model = RegimeHMM(n_init=1)
        model.fit(features)

        # Monkeypatch RegimeHMM.load to return the fitted model
        def mock_load(model_id, base=None):
            return model

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            mock_load,
        )

        # Use the timestamp from the last bar
        now = bars_df["ts"].iloc[-1].to_pydatetime()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Run classifier
        result = classify_and_store(db, now=now, lookback_days=400)

        # Assert result structure includes crisis key
        assert "crisis" in result
        assert isinstance(result["crisis"], bool)

    def test_classifier_refreshes_stale_bars_then_writes(self, db, monkeypatch):
        """Bars older than regime_max_bar_age_days trigger one index backfill;
        a successful refresh lets the classifier proceed and write."""
        # Insert synthetic bars ending 30 days before `now` (past the 7-day guard)
        bars_df = self._make_synthetic_bars("^STOXX50E", days=420)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()  # Convert Timestamp to datetime
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()

        # Build features and fit a model on the seeded history
        features = build_regime_features(
            bars_df[["ts", "close"]],
            macro_df=None,
            vol_window=21
        )
        model = RegimeHMM(n_init=1)
        model.fit(features)

        # Monkeypatch RegimeHMM.load to return the fitted model
        def mock_load(model_id, base=None):
            return model

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            mock_load,
        )

        # Use a timestamp 30 days after the last bar so the slice is stale
        now = bars_df["ts"].iloc[-1].to_pydatetime() + timedelta(days=30)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Fake ingestion entrypoint: records the requested symbol and backfills
        # fresh bars up to `now` (no network).
        calls = []

        class FakeDataIngester:
            def __init__(self, session):
                self.session = session

            def ingest_bar_prices(self, symbol, start_date=None, end_date=None, days=None):
                calls.append(symbol)
                fresh = pd.DataFrame({
                    "symbol": symbol,
                    "ts": pd.date_range(end=now.replace(tzinfo=None), periods=10, freq="D"),
                    "open": [100.0] * 10,
                    "high": [101.0] * 10,
                    "low": [99.0] * 10,
                    "close": [100.5] * 10,
                    "volume": [1_000_000] * 10,
                    "currency": "USD",
                    "provider": "test",
                })
                for _, row in fresh.iterrows():
                    row_dict = dict(row)
                    row_dict['ts'] = row_dict['ts'].to_pydatetime()
                    self.session.execute(text("""
                        INSERT INTO bar_prices
                        (symbol, ts, open, high, low, close, volume, currency, provider)
                        VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
                    """), row_dict)
                self.session.commit()
                return {"success": True, "symbol": symbol}

        monkeypatch.setattr(
            "app.lab.regime.classifier.DataIngester",
            FakeDataIngester,
        )

        result = classify_and_store(db, now=now, lookback_days=400)

        # Refresh was attempted exactly once, for the index symbol only
        assert calls == ["^STOXX50E"]

        # Snapshot written after the successful refresh
        assert result["written"] is True
        assert result["stale_bars"] is False
        assert "last_bar_ts" in result

        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 1

        payload = json.loads(
            db.execute(text("SELECT payload_json FROM regime_snapshots")).fetchone()[0]
        )
        assert payload["stale_bars"] is False
        assert "last_bar_ts" in payload

    def test_classifier_skips_write_when_refresh_fails(self, db, monkeypatch):
        """Persistently stale bars (refresh raises) → written=False,
        reason='stale_bars', no snapshot row."""
        # Insert synthetic bars ending 30 days before `now`
        bars_df = self._make_synthetic_bars("^STOXX50E", days=420)
        for _, row in bars_df.iterrows():
            row_dict = dict(row)
            row_dict['ts'] = row_dict['ts'].to_pydatetime()  # Convert Timestamp to datetime
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
            """), row_dict)
        db.commit()

        # Monkeypatch to avoid actual model load
        def mock_load(model_id, base=None):
            raise Exception("Should not be called")

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            mock_load,
        )

        now = bars_df["ts"].iloc[-1].to_pydatetime() + timedelta(days=30)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Ingestion entrypoint raises — the guard must never propagate it
        class FailingDataIngester:
            def __init__(self, session):
                self.session = session

            def ingest_bar_prices(self, symbol, start_date=None, end_date=None, days=None):
                raise RuntimeError("provider down")

        monkeypatch.setattr(
            "app.lab.regime.classifier.DataIngester",
            FailingDataIngester,
        )

        result = classify_and_store(db, now=now, lookback_days=400)

        assert result["written"] is False
        assert result["reason"] == "stale_bars"
        assert "last_bar_ts" in result

        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 0
