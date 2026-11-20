"""Regime classification via Gaussian Hidden Markov Model with deterministic labelling."""
from __future__ import annotations

import logging
import warnings
from typing import Any, cast

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler

from app.lab.quant_ml.registry import _load_model_unvalidated, save_model

logger = logging.getLogger(__name__)

LABELS = ("bull", "sideways", "bear")

# Bumped whenever the save()/load() artifact shape changes. load() refuses
# anything else outright rather than risking a partial/garbled reconstruction.
_ARTIFACT_SCHEMA_VERSION = 1


class RegimeHMM:
    """Gaussian HMM for regime classification with deterministic state→label mapping.

    Since 2026-09-28 this is the fallback behind the jump model (ADR 0004
    amendment). States are labelled by the mean of the ``vol`` feature (lowest
    → bull, highest → bear, middle → sideways) and the fit keeps the best of
    ``n_init`` seeded restarts. Ranking by mean return, from a single start,
    relabelled 48% of overlapping days at every weekly refit on real Euro
    Stoxx 50 data: return means of the three states are close and noisy,
    volatility levels are not (Shu, Yu & Mulvey 2024 separate HMM regimes by
    volatility for the same reason). Without a ``vol`` column the return
    ranking is used.

    ``n_states`` is fixed at 3, not tuned or made configurable, as a deliberate
    interpretability trade-off rather than a modelling limitation: a finer state
    count (5, 7, ...) could in principle capture more nuance, but every downstream
    consumer of a regime label (``crisis_gate.py``, advisor/discover prompts,
    dashboards) is built around exactly the bull/sideways/bear taxonomy the
    return-rank mapping above produces. More states would need their own semantic
    re-derivation each time the model refits, defeating the point of a fixed,
    human-readable label set. See ``docs/adr/0004-regime-jump-model-spike.md``
    for the wider evaluation this design sits inside.
    """

    def __init__(
        self,
        n_states: int = 3,
        covariance_type: str = "full",
        n_iter: int = 100,
        random_state: int = 42,
        return_col: str = "ret",
        min_covar: float = 0.01,
        n_init: int = 10,
        vol_col: str = "vol",
    ) -> None:
        """Initialize a regime HMM.

        Args:
            n_states: Number of hidden states (typically 3 for bull/sideways/bear).
                Must be 3 for deterministic labelling.
            covariance_type: Covariance type for GaussianHMM ('full', 'tied', 'diag', 'spherical').
            n_iter: Max iterations for EM algorithm.
            random_state: Seed for reproducibility.
            return_col: Name of the returns column in feature frames (used for state ranking).
            min_covar: Floor added to each state's estimated covariance diagonal
                (hmmlearn's ``GaussianHMM(min_covar=...)``). Features are
                StandardScaled to unit variance before fitting, so this is a
                floor in units of "fraction of overall variance". Defense in
                depth against a state's covariance collapsing to near-zero and
                producing an overconfident, sticky posterior — hmmlearn's
                default (1e-3) is too permissive for that failure mode; kept
                well below the smallest true state variance seen in the
                well-separated synthetic regimes used in tests, so it floors
                degenerate fits without blurring legitimate ones.
            n_init: Seeded restarts (``random_state + i``); the converged fit
                with the highest log-likelihood wins.
            vol_col: Feature the states are ranked by for labelling.
        """
        if n_states != 3:
            raise ValueError(
                f"RegimeHMM requires n_states=3 for deterministic labelling; got {n_states}"
            )

        self.n_states = n_states
        self.covariance_type = covariance_type
        self.n_iter = n_iter
        self.random_state = random_state
        self.return_col = return_col
        self.min_covar = min_covar
        self.n_init = max(1, int(n_init))
        self.vol_col = vol_col

        self.feature_columns: list[str] | None = None
        self.scaler: StandardScaler | None = None
        self.model: GaussianHMM | None = None
        self._state_to_label: dict[int, str] | None = None
        self._converged: bool = False

    @property
    def converged(self) -> bool:
        """Whether the most recent fit() reached EM convergence.

        False before any fit() call, and after a fit whose hmmlearn monitor
        signalled non-convergence on every attempt (including the two
        continue-from-current-params retries). Callers deciding whether to
        persist a freshly fitted model should consult this instead of the
        private attribute.
        """
        return self._converged

    def fit(self, X: pd.DataFrame) -> RegimeHMM:
        """Fit the HMM to features.

        Args:
            X: DataFrame with features (columns become self.feature_columns).
                Must include return_col for deterministic labelling.

        Returns:
            self for chaining.

        Raises:
            ValueError: if return_col is not in X.columns.
        """
        if self.return_col not in X.columns:
            raise ValueError(
                f"Feature column '{self.return_col}' not found in X.columns: {list(X.columns)}"
            )

        # Store feature column order
        self.feature_columns = list(X.columns)

        # Standardize features
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        best_model: GaussianHMM | None = None
        best_ll = -np.inf
        best_converged = False
        for i in range(self.n_init):
            candidate, converged = self._fit_once(X_scaled, self.random_state + i)
            try:
                ll = float(candidate.score(X_scaled))
            except Exception:
                ll = -np.inf
            # A converged fit always beats a non-converged one.
            if best_model is None or (converged, ll) > (best_converged, best_ll):
                best_model, best_ll, best_converged = candidate, ll, converged
        assert best_model is not None
        self.model = best_model
        self._converged = best_converged

        if not self._converged:
            logger.warning(
                "RegimeHMM EM did not converge on any of %d starts "
                "(n_iter=%d); predictions may be unreliable",
                self.n_init,
                self.n_iter,
            )

        # Deterministic state→label mapping: by volatility level when the
        # feature exists (low → bull), else by mean return (high → bull).
        if self.vol_col in self.feature_columns:
            key = self.model.means_[:, self.feature_columns.index(self.vol_col)]
            ranked = np.argsort(key)
            self._state_to_label = {
                int(ranked[0]): "bull",
                int(ranked[1]): "sideways",
                int(ranked[2]): "bear",
            }
        else:
            key = self.model.means_[:, self.feature_columns.index(self.return_col)]
            ranked = np.argsort(key)
            self._state_to_label = {
                int(ranked[2]): "bull",
                int(ranked[1]): "sideways",
                int(ranked[0]): "bear",
            }

        return self

    def _fit_once(self, X_scaled: np.ndarray, seed: int) -> tuple[GaussianHMM, bool]:
        """One seeded GaussianHMM fit with convergence hardening.

        hmmlearn raises a ConvergenceWarning when EM does not reach tolerance
        within n_iter; the model still has parameters, but predict() can
        return garbage labels. The fit is retried (continuing from current
        estimates via init_params="") up to 2 more times.
        """
        model = GaussianHMM(
            n_components=self.n_states,
            covariance_type=self.covariance_type,
            n_iter=self.n_iter,
            random_state=seed,
            min_covar=self.min_covar,
        )
        max_attempts = 3  # 1 initial + 2 retries
        for attempt in range(max_attempts):
            with warnings.catch_warnings(record=True) as caught:
                # "always" on every attempt, not just the first: catch_warnings
                # (record=True) already overrides showwarning to append instead
                # of printing, so there is no console-spam cost to recording on
                # retries too — and an "ignore" filter here doesn't merely
                # silence the message, it prevents catch_warnings from ever
                # seeing it, which used to make every retry unconditionally
                # count as converged regardless of whether EM actually
                # converged that time (the retry-detection was structurally
                # dead past attempt 0).
                warnings.simplefilter("always")
                if attempt > 0:
                    model.init_params = ""  # continue from current params
                model.fit(X_scaled)

            non_converged = any(
                "ConvergenceWarning" in type(warning).__name__
                or "converge" in str(warning.message).lower()
                for warning in caught
            )
            if not non_converged:
                return model, True
        return model, False

    def predict(self, X: pd.DataFrame) -> list[str]:
        """Predict regime labels for each row.

        Args:
            X: DataFrame with the same columns as were used in fit.

        Returns:
            List of regime labels ('bull', 'sideways', or 'bear').

        Raises:
            ValueError: if required columns are missing.
        """
        if self.model is None or self.scaler is None or self._state_to_label is None:
            raise ValueError("Model must be fit before predict")

        # Reindex X to match fitted columns
        X_aligned = self._reindex_features(X)

        # Standardize
        X_scaled = self.scaler.transform(X_aligned)

        # Predict states
        states = self.model.predict(X_scaled)

        # Map states to labels
        labels = [self._state_to_label[int(s)] for s in states]
        return labels

    def predict_state_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Predict posterior probabilities over hidden states.

        Args:
            X: DataFrame with the same columns as were used in fit.

        Returns:
            Array of shape (n_rows, n_states) with posterior probabilities.

        Raises:
            ValueError: if required columns are missing.
        """
        if self.model is None or self.scaler is None:
            raise ValueError("Model must be fit before predict_state_proba")

        # Reindex X to match fitted columns
        X_aligned = self._reindex_features(X)

        # Standardize
        X_scaled = self.scaler.transform(X_aligned)

        # Predict state probabilities
        return self.model.predict_proba(X_scaled)

    def classify_latest(self, X: pd.DataFrame) -> dict[str, Any]:
        """Classify the latest row and return probabilities.

        Args:
            X: DataFrame with features (last row is classified).

        Returns:
            Dict with:
                - label: the regime label ('bull', 'sideways', 'bear')
                - score: float in [0, 1], max posterior probability
                - probs: dict {label: float, ...} with per-label probabilities

            Raises:
            ValueError: if X is empty or model is not fit.
        """
        if self.model is None or self.scaler is None or self._state_to_label is None:
            raise ValueError("Model must be fit before classify_latest")

        if len(X) == 0:
            raise ValueError("X must not be empty")

        # Get posteriors for last row
        posteriors = self.predict_state_proba(X)
        last_posterior = posteriors[-1, :]  # shape (n_states,)

        # Find the state with highest posterior
        best_state = np.argmax(last_posterior)
        score = float(last_posterior[best_state])
        label = self._state_to_label[int(best_state)]

        # Build per-label probs by summing posteriors of states with that label
        probs: dict[str, float] = {"bull": 0.0, "sideways": 0.0, "bear": 0.0}
        for state_idx, state_label in self._state_to_label.items():
            probs[state_label] += float(last_posterior[state_idx])

        return {"label": label, "score": score, "probs": probs}

    def save(self, model_id: str, base: str | None = None) -> str:
        """Save the fitted model to disk via joblib.

        Persists a plain state dict of the fitted parameters (scaler, HMM,
        state→label map, hyperparameters) rather than pickling `self`
        directly. Pickle embeds the *module path* of any custom class it
        serialises; this class has already moved once (ADR 0015's layer
        restructure, `app.services.regime.hmm_model` -> `app.lab.regime.
        hmm_model`), which silently broke every persisted artifact's
        `joblib.load()` with `ModuleNotFoundError` until the next refit —
        the daily classifier job caught and logged it as a warning
        (classifier.py's fail-open contract) rather than raising, so
        discovery ran gated shut on a stale snapshot for two days before
        anyone noticed. A state dict only ever contains numpy/sklearn/
        hmmlearn objects and plain Python data — never this module's own
        path — so it survives `RegimeHMM` being moved again.

        Args:
            model_id: Identifier for the model (used in file path).
            base: Optional base directory (defaults to registry default).

        Returns:
            Path to saved model.

        Raises:
            ValueError: if model is not fit.
        """
        if self.model is None or self.scaler is None or self._state_to_label is None:
            raise ValueError("Model must be fit before save")

        state = {
            "schema_version": _ARTIFACT_SCHEMA_VERSION,
            "n_states": self.n_states,
            "covariance_type": self.covariance_type,
            "n_iter": self.n_iter,
            "random_state": self.random_state,
            "return_col": self.return_col,
            "min_covar": self.min_covar,
            "n_init": self.n_init,
            "vol_col": self.vol_col,
            "feature_columns": self.feature_columns,
            "scaler": self.scaler,
            "model": self.model,
            "state_to_label": self._state_to_label,
            "converged": self._converged,
        }

        kwargs = {"model_id": model_id}
        if base is not None:
            kwargs["base"] = base

        return save_model(state, **kwargs)

    @classmethod
    def load(cls, model_id: str, base: str | None = None) -> RegimeHMM:
        """Load a fitted model from disk and reconstruct it under the
        *current* class definition (see `save()` for why this doesn't
        unpickle a `RegimeHMM` instance directly).

        Args:
            model_id: Identifier of the model to load.
            base: Optional base directory (defaults to registry default).

        Returns:
            Reconstructed RegimeHMM instance.

        Raises:
            ValueError: if the artefact is missing, not a state dict, or
                was written by an incompatible schema version — never
                partially reconstructs from an artefact it doesn't
                recognise.
        """
        # _load_model_unvalidated: the state dict's values (StandardScaler,
        # GaussianHMM) aren't a sklearn Pipeline, so the registry's default
        # Pipeline-shaped safety check doesn't apply; safety here instead
        # comes from the dict containing only trusted external-library
        # objects (see save()), never an app-defined class.
        kwargs = {"model_id": model_id}
        if base is not None:
            kwargs["base"] = base

        state = _load_model_unvalidated(**kwargs)
        if not isinstance(state, dict) or state.get("schema_version") != _ARTIFACT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported RegimeHMM artefact for model_id={model_id!r} "
                f"(expected a schema_version={_ARTIFACT_SCHEMA_VERSION} state dict); "
                "refit and re-save under the current code before loading it again"
            )

        instance = cls(
            n_states=state["n_states"],
            covariance_type=state["covariance_type"],
            n_iter=state["n_iter"],
            random_state=state["random_state"],
            return_col=state["return_col"],
            min_covar=state["min_covar"],
            n_init=state.get("n_init", 1),
            vol_col=state.get("vol_col", "vol"),
        )
        instance.feature_columns = state["feature_columns"]
        instance.scaler = state["scaler"]
        instance.model = state["model"]
        instance._state_to_label = state["state_to_label"]
        instance._converged = state["converged"]
        return instance

    def _reindex_features(self, X: pd.DataFrame) -> pd.DataFrame:
        """Reindex X to match fitted feature columns.

        Args:
            X: DataFrame to reindex.

        Returns:
            DataFrame with columns in self.feature_columns order.

        Raises:
            ValueError: if any required column is missing.
        """
        if self.feature_columns is None:
            raise ValueError("Model must be fit before _reindex_features")

        missing = set(self.feature_columns) - set(X.columns)
        if missing:
            raise ValueError(
                f"Missing required columns: {missing}. "
                f"Expected: {self.feature_columns}"
            )

        return cast(pd.DataFrame, X[self.feature_columns])
