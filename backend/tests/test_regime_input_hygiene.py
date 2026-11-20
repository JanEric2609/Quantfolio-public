"""Tests for numerical input-hygiene guards in the regime HMM fit/classify path.

Covers audit item T4.1 (docs/archive/audits/2026-08-universe-regime-audit.md, D2):
- Non-finite (NaN/±inf) feature rows are dropped and counted before
  classification/refit proceeds on the remainder.
- Too few clean rows → defensive refusal (no snapshot written, no fit).
- Constant feature columns → degenerate_features refusal on refit
  (StandardScaler would divide by ~zero ⇒ degenerate posteriors).
- EM non-convergence → refit refuses to overwrite the persisted model,
  surfaced via the public RegimeHMM.converged property.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.lab.regime.classifier import (
    _validate_feature_matrix,
    classify_and_store,
    refit_regime_model,
)
from app.lab.regime.features import build_regime_features as _real_build_features
from app.lab.regime.hmm_model import RegimeHMM

@pytest.fixture(autouse=True)
def _no_index_backfill(monkeypatch):
    """Refit backfills a short index history from the network; these tests
    seed their own bars."""
    monkeypatch.setattr(
        "app.lab.regime.classifier._ensure_index_history",
        lambda db, symbol, bars_df, *, start, end: bars_df,
    )



def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
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
    return _memory_db()


def _make_synthetic_bars(symbol: str, days: int = 420) -> pd.DataFrame:
    """Synthetic random-walk OHLCV bars ending today (passes freshness guard)."""
    np.random.seed(42)
    end_date = datetime.now(timezone.utc).date()
    start_date = pd.Timestamp(end_date) - pd.Timedelta(days=days - 1)
    dates = pd.date_range(start=start_date, periods=days, freq="D")
    returns = np.random.randn(days) * 0.01
    close = 100 * np.exp(np.cumsum(returns))
    return pd.DataFrame({
        "symbol": symbol,
        "ts": dates,
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": 1_000_000,
        "currency": "USD",
        "provider": "test",
    })


def _seed_bars(db, bars_df: pd.DataFrame) -> None:
    for _, row in bars_df.iterrows():
        row_dict = dict(row)
        row_dict["ts"] = row_dict["ts"].to_pydatetime()
        db.execute(text("""
            INSERT INTO bar_prices
            (symbol, ts, open, high, low, close, volume, currency, provider)
            VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :currency, :provider)
        """), row_dict)
    db.commit()


def _fit_model_on(bars_df: pd.DataFrame) -> RegimeHMM:
    features = _real_build_features(
        bars_df[["ts", "close"]], macro_df=None, vol_window=21
    )
    model = RegimeHMM(n_init=1)
    model.fit(features)
    return model


def _poison(frame: pd.DataFrame) -> pd.DataFrame:
    """Copy of the frame with exactly one NaN row and one ±inf row injected."""
    bad = frame.copy()
    bad.iloc[-10, bad.columns.get_loc("ret")] = np.nan
    bad.iloc[-20, bad.columns.get_loc("vol")] = np.inf
    return bad


# ============================================================================
# Unit tests for _validate_feature_matrix
# ============================================================================


class TestValidateFeatureMatrix:
    def test_drops_nan_and_inf_rows_and_reports_count(self):
        frame = pd.DataFrame({
            "ret": [0.01, np.nan, 0.02, np.inf, -0.01],
            "vol": [0.1, 0.1, 0.2, 0.2, 0.1],
            "drawdown": [-0.05, -0.05, -0.10, -0.10, -0.02],
        })
        cleaned, report = _validate_feature_matrix(frame)

        assert report["dropped"] == 2
        assert len(cleaned) == 3
        assert np.isfinite(cleaned.to_numpy(dtype=float)).all()

    def test_flags_constant_columns_on_cleaned_frame(self):
        frame = pd.DataFrame({
            "ret": [0.01, 0.02, 0.03, 0.04],
            "vol": [0.1, 0.2, 0.1, 0.2],
            "drawdown": [0.0, 0.0, 0.0, 0.0],  # constant ⇒ degenerate
        })
        _, report = _validate_feature_matrix(frame)

        assert report["constant_columns"] == ["drawdown"]

    def test_clean_frame_reports_zero_dropped_and_no_constant_columns(self):
        frame = pd.DataFrame({
            "ret": [0.01, -0.02, 0.03],
            "vol": [0.1, 0.2, 0.15],
            "drawdown": [-0.05, -0.10, -0.02],
        })
        cleaned, report = _validate_feature_matrix(frame)

        assert report["dropped"] == 0
        assert report["constant_columns"] == []
        assert len(cleaned) == 3

    def test_empty_frame_is_handled(self):
        cleaned, report = _validate_feature_matrix(pd.DataFrame())

        assert report["dropped"] == 0
        assert report["constant_columns"] == []
        assert len(cleaned) == 0


# ============================================================================
# classify_and_store hygiene guards
# ============================================================================


class TestClassifyHygiene:
    def test_classify_drops_nonfinite_rows_and_proceeds(self, db, monkeypatch):
        """One NaN row + one inf row are dropped; classification still writes."""
        bars_df = _make_synthetic_bars("^STOXX50E", days=420)
        _seed_bars(db, bars_df)

        model = _fit_model_on(bars_df)
        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load",
            lambda model_id, base=None: model,
        )

        clean = _real_build_features(
            bars_df[["ts", "close"]], macro_df=None, vol_window=21
        )
        poisoned = _poison(clean)
        monkeypatch.setattr(
            "app.lab.regime.classifier.build_regime_features",
            lambda *a, **k: poisoned,
        )

        now = bars_df["ts"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        result = classify_and_store(db, now=now, lookback_days=400)

        assert result["written"] is True, f"expected write, got: {result}"
        assert result["dropped_nonfinite_rows"] == 2
        assert result["label"] in ("bull", "sideways", "bear")

        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 1

    def test_classify_refuses_when_all_rows_nonfinite(self, db, monkeypatch):
        """All-NaN feature frame → insufficient_clean_features, nothing written,
        and the persisted model is never even loaded."""
        bars_df = _make_synthetic_bars("^STOXX50E", days=420)
        _seed_bars(db, bars_df)

        def exploding_load(model_id, base=None):
            raise AssertionError("model must not be loaded on a refused frame")

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load", exploding_load
        )

        clean = _real_build_features(
            bars_df[["ts", "close"]], macro_df=None, vol_window=21
        )
        all_nan = clean.astype(float) * np.nan
        monkeypatch.setattr(
            "app.lab.regime.classifier.build_regime_features",
            lambda *a, **k: all_nan,
        )

        now = bars_df["ts"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        result = classify_and_store(db, now=now, lookback_days=400)

        assert result["written"] is False
        assert result["reason"] == "insufficient_clean_features"
        assert result["dropped_nonfinite_rows"] == len(all_nan)

        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 0

    def test_classify_refuses_when_too_few_clean_rows(self, db, monkeypatch):
        """A finite but tiny (<30 rows) feature frame is refused outright."""
        bars_df = _make_synthetic_bars("^STOXX50E", days=420)
        _seed_bars(db, bars_df)

        def exploding_load(model_id, base=None):
            raise AssertionError("model must not be loaded on a refused frame")

        monkeypatch.setattr(
            "app.lab.regime.classifier.RegimeHMM.load", exploding_load
        )

        rng = np.random.default_rng(7)
        small = pd.DataFrame({
            "ret": rng.normal(0, 0.01, size=25),
            "vol": rng.uniform(0.005, 0.02, size=25),
            "drawdown": rng.uniform(-0.1, 0.0, size=25),
        })
        monkeypatch.setattr(
            "app.lab.regime.classifier.build_regime_features",
            lambda *a, **k: small,
        )

        now = bars_df["ts"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        result = classify_and_store(db, now=now, lookback_days=400)

        assert result["written"] is False
        assert result["reason"] == "insufficient_clean_features"
        assert result["dropped_nonfinite_rows"] == 0

        count = db.execute(text("SELECT COUNT(*) FROM regime_snapshots")).scalar()
        assert count == 0


# ============================================================================
# refit_regime_model hygiene guards
# ============================================================================


class TestRefitHygiene:
    def test_refit_drops_nonfinite_rows_and_proceeds(self, db, monkeypatch, tmp_path):
        """Non-finite rows are dropped; refit fits and saves on the remainder."""
        bars_df = _make_synthetic_bars("^STOXX50E", days=450)
        _seed_bars(db, bars_df)

        clean = _real_build_features(
            bars_df[["ts", "close"]], macro_df=None, vol_window=21
        )
        poisoned = _poison(clean)
        monkeypatch.setattr(
            "app.lab.regime.classifier.build_regime_features",
            lambda *a, **k: poisoned,
        )

        result = refit_regime_model(db, base=str(tmp_path))

        assert result["saved"] is True, f"expected save, got: {result}"
        assert result["dropped_nonfinite_rows"] == 2
        assert result["n_rows"] == len(clean) - 2

        assert any(f.is_file() for f in tmp_path.rglob("*"))

    def test_refit_refuses_insufficient_clean_rows(self, db, monkeypatch, tmp_path):
        """All-NaN frame → min_rows check fires on the cleaned (empty) frame;
        no model file is written."""
        bars_df = _make_synthetic_bars("^STOXX50E", days=450)
        _seed_bars(db, bars_df)

        clean = _real_build_features(
            bars_df[["ts", "close"]], macro_df=None, vol_window=21
        )
        all_nan = clean.astype(float) * np.nan
        monkeypatch.setattr(
            "app.lab.regime.classifier.build_regime_features",
            lambda *a, **k: all_nan,
        )

        result = refit_regime_model(db, base=str(tmp_path))

        assert result["saved"] is False
        assert result["n_rows"] == 0
        assert "need at least 120" in result["reason"]
        assert list(tmp_path.rglob("*")) == []

    def test_refit_refuses_degenerate_constant_column(self, db, monkeypatch, tmp_path):
        """A constant feature column (std < 1e-12) → degenerate_features refusal;
        fit is never attempted and nothing is saved."""
        bars_df = _make_synthetic_bars("^STOXX50E", days=450)
        _seed_bars(db, bars_df)

        rng = np.random.default_rng(11)
        degenerate = pd.DataFrame({
            "ret": rng.normal(0, 0.01, size=150),
            "vol": rng.uniform(0.005, 0.02, size=150),
            # Frozen rolling peak ⇒ zero-variance drawdown (the prod failure).
            "drawdown": np.full(150, -0.05),
        })
        monkeypatch.setattr(
            "app.lab.regime.classifier.build_regime_features",
            lambda *a, **k: degenerate,
        )

        result = refit_regime_model(db, base=str(tmp_path))

        assert result["saved"] is False
        assert result["reason"] == "degenerate_features"
        assert result["constant_columns"] == ["drawdown"]
        assert result["n_rows"] == 150
        assert list(tmp_path.rglob("*")) == []


# ============================================================================
# Convergence surfacing
# ============================================================================


class TestConvergenceSurfacing:
    def test_converged_property_false_before_fit_true_after(self):
        """The public property mirrors the private flag across a real fit."""
        np.random.seed(42)
        dates = pd.date_range("2020-01-01", periods=420, freq="D")
        close = 100 * np.exp(np.cumsum(np.random.randn(420) * 0.01))
        bars = pd.DataFrame({"ts": dates, "close": close})
        features = _real_build_features(bars, macro_df=None, vol_window=21)

        model = RegimeHMM(n_init=1)
        assert model.converged is False

        model.fit(features)
        assert model.converged is True

    def test_refit_refuses_to_save_on_non_convergence(self, db, monkeypatch, tmp_path):
        """A fit whose EM never converges must not overwrite the persisted
        model: refit returns saved=False / reason='not_converged' and never
        calls save."""
        bars_df = _make_synthetic_bars("^STOXX50E", days=450)
        _seed_bars(db, bars_df)

        real_fit = RegimeHMM.fit

        def never_converging_fit(self, X):
            real_fit(self, X)
            self._converged = False  # force the monitor outcome
            return self

        monkeypatch.setattr(RegimeHMM, "fit", never_converging_fit)

        save_calls = []

        def spy_save(self, model_id, base=None):
            save_calls.append(model_id)
            return "should-not-be-written"

        monkeypatch.setattr(RegimeHMM, "save", spy_save)

        result = refit_regime_model(db, base=str(tmp_path))

        assert result["saved"] is False
        assert result["reason"] == "not_converged"
        assert save_calls == []
        # No HMM artefact; the jump model is refitted independently of the
        # HMM's convergence guard.
        assert not (tmp_path / "regime_hmm").exists()
        assert result["jump"]["saved"] is True
