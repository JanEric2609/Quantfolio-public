"""Unit tests for regime HMM and feature builder.

Tests cover feature construction, HMM fitting, deterministic labelling,
and save/load round-trips.
"""

import tempfile
import warnings

import numpy as np
import pandas as pd
import pytest

from app.lab.regime.features import build_regime_features
from app.lab.regime.hmm_model import RegimeHMM

@pytest.fixture(autouse=True)
def _no_index_backfill(monkeypatch):
    """Refit backfills a short index history from the network; these tests
    seed their own bars."""
    monkeypatch.setattr(
        "app.lab.regime.classifier._ensure_index_history",
        lambda db, symbol, bars_df, *, start, end: bars_df,
    )



class TestBuildRegimeFeatures:
    """Tests for the regime feature builder."""

    def test_basic_features(self):
        """Test that basic features are computed correctly."""
        # Create synthetic price data
        dates = pd.date_range("2020-01-01", periods=100, freq="D")
        close = 100 + np.cumsum(np.random.randn(100) * 0.5)

        prices_df = pd.DataFrame({"ts": dates, "close": close})

        # Build features
        features = build_regime_features(prices_df, vol_window=21)

        # Check structure
        assert isinstance(features, pd.DataFrame)
        assert isinstance(features.index, pd.DatetimeIndex)
        assert set(features.columns) == {"ret", "vol", "drawdown"}
        assert "close" not in features.columns

        # Check lengths: should drop warmup (vol_window - 1) rows
        assert len(features) == len(prices_df) - 21

        # Check no NaNs
        assert features.isnull().sum().sum() == 0

        # Check drawdown is always <= 0
        assert (features["drawdown"] <= 0).all()

    def test_missing_required_columns(self):
        """Test that ValueError is raised for missing columns."""
        prices_df = pd.DataFrame({"ts": pd.date_range("2020-01-01", periods=10)})
        with pytest.raises(ValueError, match="must contain columns"):
            build_regime_features(prices_df)

    def test_with_macro_features(self):
        """Test that macro features are merged correctly."""
        # Create price data
        dates = pd.date_range("2020-01-01", periods=100, freq="D")
        close = 100 + np.cumsum(np.random.randn(100) * 0.5)
        prices_df = pd.DataFrame({"ts": dates, "close": close})

        # Create macro data (sparse)
        macro_dates = pd.date_range("2020-01-01", periods=50, freq="2D")
        macro_df = pd.DataFrame(
            {
                "ts": macro_dates,
                "vix": 15 + np.random.randn(50) * 2,
                "yield_slope": 0.5 + np.random.randn(50) * 0.1,
            }
        )

        # Build features
        features = build_regime_features(prices_df, macro_df, vol_window=21)

        # Check that vix and yield_slope are present
        assert "vix" in features.columns
        assert "yield_slope" in features.columns
        assert "close" not in features.columns

        # Check exact set of columns
        assert set(features.columns) == {"ret", "vol", "drawdown", "vix", "yield_slope"}

        # Check no NaNs in the merged data
        assert features.isnull().sum().sum() == 0

    def test_retindex_matches_input(self):
        """Test that returns are computed correctly after warmup drop.

        With vol_window=2, rolling std requires 2 valid ret values.
        Because ret[0] is NaN (pct_change), the rolling(2).std() of
        [NaN, ret1] is also NaN, so the first two rows are dropped.
        The first surviving row has close=99 (third row).
        """
        close = np.array([100, 101, 99, 102], dtype=float)
        dates = pd.date_range("2020-01-01", periods=4, freq="D")
        prices_df = pd.DataFrame({"ts": dates, "close": close})

        features = build_regime_features(prices_df, vol_window=2)

        # vol_window=2 drops 2 rows (ret[0]=NaN; vol[1]=NaN because it windows
        # over [NaN, ret1]). First surviving row corresponds to close=99.
        expected_ret_0 = (99 - 101) / 101   # close[2] vs close[1]
        expected_ret_1 = (102 - 99) / 99     # close[3] vs close[2]

        assert len(features) == 2
        assert np.isclose(features["ret"].iloc[0], expected_ret_0)
        assert np.isclose(features["ret"].iloc[1], expected_ret_1)

    def test_with_macro_features_tz_mismatch(self):
        """Regression test: tz-aware prices (Postgres TIMESTAMPTZ, as returned
        by BarStore.get_bars() in production) merged against tz-naive macro
        data (as built by classifier._build_macro_frame) must not raise
        pandas.errors.MergeError. This crashed the daily regime job in
        production before build_regime_features started aligning tz-awareness.
        """
        dates = pd.date_range("2020-01-01", periods=100, freq="D", tz="UTC")
        close = 100 + np.cumsum(np.random.randn(100) * 0.5)
        prices_df = pd.DataFrame({"ts": dates, "close": close})

        macro_dates = pd.date_range("2020-01-01", periods=50, freq="2D")  # tz-naive
        macro_df = pd.DataFrame(
            {
                "ts": macro_dates,
                "vix": 15 + np.random.randn(50) * 2,
            }
        )

        features = build_regime_features(prices_df, macro_df, vol_window=21)

        assert "vix" in features.columns
        assert features.isnull().sum().sum() == 0

    def test_with_macro_features_reverse_tz_mismatch(self):
        """Same as above but with the tz-awareness reversed (tz-naive prices,
        tz-aware macro), as could happen if a caller builds macro_df with
        explicit UTC timestamps against a tz-naive (e.g. SQLite-backed) price
        frame.
        """
        dates = pd.date_range("2020-01-01", periods=100, freq="D")  # tz-naive
        close = 100 + np.cumsum(np.random.randn(100) * 0.5)
        prices_df = pd.DataFrame({"ts": dates, "close": close})

        macro_dates = pd.date_range("2020-01-01", periods=50, freq="2D", tz="UTC")
        macro_df = pd.DataFrame(
            {
                "ts": macro_dates,
                "vix": 15 + np.random.randn(50) * 2,
            }
        )

        features = build_regime_features(prices_df, macro_df, vol_window=21)

        assert "vix" in features.columns
        assert features.isnull().sum().sum() == 0


class TestRegimeHMM:
    """Tests for the RegimeHMM model."""

    @staticmethod
    def create_synthetic_regime_data(
        bull_size: int = 300,
        sideways_size: int = 300,
        bear_size: int = 300,
        seed: int = 42,
    ) -> pd.DataFrame:
        """Create synthetic 3-regime price data.

        Three contiguous blocks of daily returns with distinct characteristics.
        Regimes are intentionally well-separated so the HMM can recover them:
          - Bull:     high positive mean (+0.0040), low volatility (0.003)
          - Sideways: near-zero mean (0.0),         medium volatility (0.006)
          - Bear:     negative mean (-0.0050),       high volatility (0.025)
        """
        rng = np.random.default_rng(seed)

        # Generate returns for each regime — well-separated for reliable HMM recovery
        # Increased separation: wider mean spreads and more differentiated volatility
        bull_rets = rng.normal(loc=0.0040, scale=0.003, size=bull_size)
        sideways_rets = rng.normal(loc=0.0, scale=0.006, size=sideways_size)
        bear_rets = rng.normal(loc=-0.0050, scale=0.025, size=bear_size)

        returns = np.concatenate([bull_rets, sideways_rets, bear_rets])

        # Convert returns to price levels
        close = 100 * np.cumprod(1 + returns)

        # Build DataFrame with ts and close
        dates = pd.date_range("2015-01-01", periods=len(close), freq="D")
        prices_df = pd.DataFrame({"ts": dates, "close": close})

        # Build features
        features = build_regime_features(prices_df, vol_window=21)

        return features

    def test_hmm_fit_and_predict(self):
        """Test that HMM can fit and predict."""
        features = self.create_synthetic_regime_data()

        hmm = RegimeHMM(n_states=3, random_state=42, n_init=1)
        hmm.fit(features)

        # Check that model is fit
        assert hmm.model is not None
        assert hmm.scaler is not None
        assert hmm._state_to_label is not None

        # Predict
        labels = hmm.predict(features)

        # Check predictions
        assert len(labels) == len(features)
        assert all(label in ("bull", "sideways", "bear") for label in labels)

    def test_regime_detection_accuracy(self):
        """Test that regimes are detected with >60% accuracy in each block."""
        features = self.create_synthetic_regime_data()

        hmm = RegimeHMM(n_states=3, n_iter=300, random_state=42, n_init=1)
        hmm.fit(features)

        labels = hmm.predict(features)

        # Note: actual sizes are slightly less due to dropped NaN rows
        # We'll estimate based on the predictions
        total_non_nan = len(features)
        third = total_non_nan // 3

        bull_block = labels[:third]
        sideways_block = labels[third : 2 * third]
        bear_block = labels[2 * third :]

        # Check that each block's modal label matches expected regime
        bull_modal = max(set(bull_block), key=bull_block.count)
        sideways_modal = max(set(sideways_block), key=sideways_block.count)
        bear_modal = max(set(bear_block), key=bear_block.count)

        bull_accuracy = bull_block.count(bull_modal) / len(bull_block)
        sideways_accuracy = (
            sideways_block.count(sideways_modal) / len(sideways_block)
        )
        bear_accuracy = bear_block.count(bear_modal) / len(bear_block)

        # Assert >60% accuracy per block
        assert bull_accuracy > 0.6, f"Bull accuracy {bull_accuracy} <= 0.6"
        assert (
            sideways_accuracy > 0.6
        ), f"Sideways accuracy {sideways_accuracy} <= 0.6"
        assert bear_accuracy > 0.6, f"Bear accuracy {bear_accuracy} <= 0.6"

    def test_states_are_labelled_by_volatility(self):
        """Low-vol state → bull, high-vol → bear (ADR 0004 amendment): mean
        returns of the three states are close and noisy, volatility is not."""
        features = self.create_synthetic_regime_data()
        hmm = RegimeHMM(n_states=3, random_state=42, n_init=1).fit(features)

        vol_idx = hmm.feature_columns.index("vol")
        by_label = {label: hmm.model.means_[state, vol_idx] for state, label in hmm._state_to_label.items()}
        assert by_label["bull"] < by_label["sideways"] < by_label["bear"]

    def test_restarts_keep_the_best_likelihood(self):
        features = self.create_synthetic_regime_data()
        single = RegimeHMM(n_states=3, random_state=42, n_init=1).fit(features)
        several = RegimeHMM(n_states=3, random_state=42, n_init=4).fit(features)

        scaled = several.scaler.transform(features)
        assert several.model.score(scaled) >= single.model.score(single.scaler.transform(features)) - 1e-6

    def test_deterministic_labelling(self):
        """Test that two models with same seed produce identical predictions."""
        features = self.create_synthetic_regime_data()

        hmm1 = RegimeHMM(n_states=3, random_state=42, n_init=1)
        hmm1.fit(features)
        labels1 = hmm1.predict(features)

        hmm2 = RegimeHMM(n_states=3, random_state=42, n_init=1)
        hmm2.fit(features)
        labels2 = hmm2.predict(features)

        # Predictions should be identical
        assert labels1 == labels2

    def test_predict_state_proba(self):
        """Test posterior probability predictions."""
        features = self.create_synthetic_regime_data()

        hmm = RegimeHMM(n_states=3, random_state=42, n_init=1)
        hmm.fit(features)

        proba = hmm.predict_state_proba(features)

        # Check shape
        assert proba.shape == (len(features), 3)

        # Check that each row sums to ~1
        row_sums = proba.sum(axis=1)
        assert np.allclose(row_sums, 1.0, atol=1e-6)

        # Check that values are in [0, 1]
        assert (proba >= 0).all() and (proba <= 1).all()

    def test_classify_latest(self):
        """Test classification of the latest row."""
        features = self.create_synthetic_regime_data()

        hmm = RegimeHMM(n_states=3, random_state=42, n_init=1)
        hmm.fit(features)

        result = hmm.classify_latest(features)

        # Check structure
        assert "label" in result
        assert "score" in result
        assert "probs" in result

        # Check label
        assert result["label"] in ("bull", "sideways", "bear")

        # Check score
        assert 0 <= result["score"] <= 1

        # Check probs
        assert set(result["probs"].keys()) == {"bull", "sideways", "bear"}
        prob_sum = sum(result["probs"].values())
        assert np.isclose(prob_sum, 1.0, atol=1e-6)

    def test_save_load_roundtrip(self):
        """Test that save/load preserves predictions."""
        features = self.create_synthetic_regime_data()

        hmm_orig = RegimeHMM(n_states=3, random_state=42, n_init=1)
        hmm_orig.fit(features)
        labels_orig = hmm_orig.predict(features)

        # Save and load
        with tempfile.TemporaryDirectory() as tmpdir:
            hmm_orig.save("test_regime", base=tmpdir)
            hmm_loaded = RegimeHMM.load("test_regime", base=tmpdir)

            labels_loaded = hmm_loaded.predict(features)

            # Check that predictions match
            assert labels_loaded == labels_orig

            # Reconstruction went through cls(...) + attribute assignment in
            # load(), not raw unpickling of a RegimeHMM instance — confirm
            # the loaded object actually has real hmmlearn/sklearn objects,
            # not e.g. a dict masquerading as one.
            from hmmlearn.hmm import GaussianHMM
            from sklearn.preprocessing import StandardScaler

            assert isinstance(hmm_loaded.model, GaussianHMM)
            assert isinstance(hmm_loaded.scaler, StandardScaler)

    def test_load_rejects_legacy_or_unversioned_artifact(self, tmp_path):
        """Regression test for the production incident where a RegimeHMM
        instance pickled directly (the pre-fix save() shape) became
        permanently unloadable the moment its module was renamed by a
        refactor (ADR 0015): joblib.load raised ModuleNotFoundError deep
        inside the daily classifier job, silently swallowed by its
        fail-open exception handler.

        save()/load() now round-trip a plain state dict instead (see
        hmm_model.py docstrings), which can never embed this module's own
        path. This test locks in the other half of that fix: load() must
        refuse a differently-shaped or unversioned artifact with a clear
        ValueError instead of a raw KeyError/AttributeError once inside the
        reconstruction — so the classifier's `model_unavailable: {e}`
        message stays actionable no matter what produced the bad artefact.
        """
        import joblib

        from app.lab.quant_ml.registry import model_path

        # Not a dict at all (simulates an old raw-instance-shaped artefact
        # from a module path that still happens to resolve).
        p = model_path("legacy_model", base=str(tmp_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(object(), p)

        with pytest.raises(ValueError, match="Unsupported RegimeHMM artefact"):
            RegimeHMM.load("legacy_model", base=str(tmp_path))

        # A dict that's missing/mismatches schema_version (simulates a
        # future format bump loaded by older code, or plain corruption).
        p2 = model_path("unversioned_model", base=str(tmp_path))
        p2.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"n_states": 3}, p2)

        with pytest.raises(ValueError, match="Unsupported RegimeHMM artefact"):
            RegimeHMM.load("unversioned_model", base=str(tmp_path))

    def test_invalid_n_states(self):
        """Test that n_states != 3 raises ValueError."""
        features = self.create_synthetic_regime_data()

        with pytest.raises(ValueError, match="n_states=3"):
            hmm = RegimeHMM(n_states=2, n_init=1)
            hmm.fit(features)

    def test_missing_required_column(self):
        """Test that missing return column raises ValueError."""
        features = self.create_synthetic_regime_data()
        features_no_ret = features.drop(columns=["ret"])

        hmm = RegimeHMM(n_states=3, return_col="ret", n_init=1)

        with pytest.raises(ValueError, match="not found"):
            hmm.fit(features_no_ret)

    def test_predict_without_fit(self):
        """Test that predict before fit raises ValueError."""
        features = self.create_synthetic_regime_data()

        hmm = RegimeHMM(n_states=3, n_init=1)

        with pytest.raises(ValueError, match="must be fit"):
            hmm.predict(features)

    def test_classify_latest_empty_frame(self):
        """Test that empty frame raises ValueError."""
        hmm = RegimeHMM(n_states=3, random_state=42, n_init=1)
        features = self.create_synthetic_regime_data()
        hmm.fit(features)

        empty_features = features.iloc[:0]

        with pytest.raises(ValueError, match="must not be empty"):
            hmm.classify_latest(empty_features)

    def test_fit_reports_non_convergence_via_its_own_retry_loop(self, monkeypatch):
        """RegimeHMM.fit()'s own ConvergenceWarning-detection/retry loop
        (hmm_model.py, up to 2 continue-from-current-params retries) must
        end with converged=False when EM genuinely never reaches tolerance
        on any attempt — exercised here via an injected warning on hmmlearn's
        own GaussianHMM.fit rather than the private _converged flag directly
        (test_regime_input_hygiene.py's refit test does the latter, for
        classifier.py's separate refusal-to-persist path; this test is
        upstream of that, at the model's own detection mechanism).

        Real EM non-convergence isn't reliably reproducible via n_iter=1
        alone across hmmlearn versions/data (the 3-attempt retry loop, each
        continuing from current params, tends to converge anyway on this
        module's synthetic fixtures) — injecting the warning message
        hmm_model.py's own detector matches on keeps this deterministic
        while still exercising that detector for real, not just its output.
        """
        from hmmlearn.hmm import GaussianHMM

        features = self.create_synthetic_regime_data()
        real_fit = GaussianHMM.fit
        attempts: list[int] = []

        def never_converging_fit(self, X, lengths=None):
            attempts.append(1)
            real_fit(self, X, lengths=lengths)
            warnings.warn("EM algorithm did not converge", RuntimeWarning)
            return self

        monkeypatch.setattr(GaussianHMM, "fit", never_converging_fit)

        hmm = RegimeHMM(n_states=3, n_iter=5, random_state=42, n_init=2)
        hmm.fit(features)

        assert hmm.converged is False
        # Per start: 1 initial attempt + 2 continue-from-current-params retries.
        assert len(attempts) == 6

    def test_refit_refuses_to_persist_when_fit_never_converges(self, tmp_path, monkeypatch):
        """The classifier's refusal-to-persist path (classifier.py's
        refit_regime_model) must never overwrite the persisted model when
        RegimeHMM.fit() itself — via its real retry loop, not a forced
        attribute — ends with converged=False."""
        from datetime import datetime, timezone

        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.foundation.core.db import Base
        from app.lab.regime.classifier import refit_regime_model
        from hmmlearn.hmm import GaussianHMM

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
        db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

        # Random-walk bars ending today, matching the classifier's
        # freshness guard (test_regime_input_hygiene.py's fixture shape).
        rng = np.random.default_rng(11)
        n = 450
        end_date = datetime.now(timezone.utc).date()
        start_date = pd.Timestamp(end_date) - pd.Timedelta(days=n - 1)
        dates = pd.date_range(start=start_date, periods=n, freq="D")
        returns = rng.normal(0.0, 0.01, n)
        close = 100 * np.exp(np.cumsum(returns))
        for dt, px in zip(dates, close):
            db.execute(text("""
                INSERT INTO bar_prices
                (symbol, ts, open, high, low, close, volume, currency, provider)
                VALUES ('^STOXX50E', :ts, :px, :px, :px, :px, 1000000, 'USD', 'test')
            """), {"ts": dt.to_pydatetime(), "px": float(px)})
        db.commit()

        real_fit = GaussianHMM.fit

        def never_converging_fit(self, X, lengths=None):
            real_fit(self, X, lengths=lengths)
            warnings.warn("EM algorithm did not converge", RuntimeWarning)
            return self

        monkeypatch.setattr(GaussianHMM, "fit", never_converging_fit)

        save_calls: list[str] = []
        real_save = RegimeHMM.save

        def spy_save(self, model_id, base=None):
            save_calls.append(model_id)
            return real_save(self, model_id, base=base)

        monkeypatch.setattr(RegimeHMM, "save", spy_save)

        result = refit_regime_model(db, base=str(tmp_path), lookback_days=750, min_rows=120)

        assert result["saved"] is False
        assert result["reason"] == "not_converged"
        assert save_calls == []
