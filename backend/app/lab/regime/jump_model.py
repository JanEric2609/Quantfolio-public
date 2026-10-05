"""Statistical jump model regime classifier — the production regime model.

Adopted on 2026-09-28 (ADR 0004 amendment) after a real-data comparison. On
weekly refits over ^STOXX50E and FRED macro features, 2023-06 to 2026-09:

- The HMM as run in prod relabelled 48% of overlapping days at every refit
  and flipped 112 times a year.
- This model (jump penalty 10, volatility-ordered labels) relabelled 6% and
  flipped 5.7 times a year.
- Through the April 2025 sell-off it stayed "bear". The HMM flip-flopped
  daily between bull and bear.

The jump model clusters the feature rows and pays ``jump_penalty`` for every
state change, which is what makes its regimes persistent (Nystrup,
Lindström & Madsen 2020; Shu, Yu & Mulvey 2024, arXiv 2402.05272).
:class:`~app.lab.regime.hmm_model.RegimeHMM` stays as the fallback when
``jumpmodels`` or this model's artefact is unavailable.

Public surface mirrors ``RegimeHMM``: fit / predict / predict_state_proba /
classify_latest / save / load / feature_columns / converged.
"""
from __future__ import annotations

import logging
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

from app.lab.quant_ml.registry import _load_model_unvalidated, save_model

logger = logging.getLogger(__name__)

LABELS = ("bull", "sideways", "bear")

# Bumped whenever the save()/load() artifact shape changes; load() refuses
# anything else rather than reconstructing a model it does not recognise.
# (The optional "reliability" key was added without a bump: a v1 artefact
# without it loads and scores with the DP softmax until its next refit.)
_ARTIFACT_SCHEMA_VERSION = 1

# Run-length buckets for the label-reliability table: days since the online
# label last changed, 0-4 / 5-20 / 21+ (see _reliability_table).
_RUN_BUCKETS = (5, 21)

_INSTALL_HINT = (
    "jumpmodels is required for JumpRegimeModel but is not installed "
    "(pip install jumpmodels==0.1.1, a main dependency since ADR 0004's "
    "2026-09-28 amendment). The classifier falls back to RegimeHMM."
)


def _import_jump_model() -> Any:
    """Lazily import and return the upstream JumpModel class.

    Single lazy-import site, so importing this module never requires the
    package and a missing install degrades to the HMM fallback instead of
    breaking every importer.
    """
    from jumpmodels.jump import JumpModel

    return JumpModel


class JumpRegimeModel:
    """Jump model for regime classification with deterministic state→label mapping.

    Features are StandardScaled before fitting. States are labelled by their
    centroid on the ``vol`` feature: lowest → bull, highest → bear, middle →
    sideways. Ranking centroids by mean return instead relabelled 13.5% of
    days per refit against 3.4% for the volatility ranking (penalty 50, same
    data), because the return means of the three states are close and noisy
    and their volatility levels are not. Without a ``vol`` column the return
    ranking is used.
    """

    def __init__(
        self,
        n_states: int = 3,
        jump_penalty: float = 10.0,
        max_iter: int = 1000,
        random_state: int = 42,
        return_col: str = "ret",
        n_init: int = 10,
        vol_col: str = "vol",
    ) -> None:
        """Initialize a jump-model regime classifier.

        Args:
            n_states: Number of latent regimes. Must be 3 for deterministic
                labelling (same constraint as RegimeHMM).
            jump_penalty: Cost of one state change, in units of the
                standardised squared-distance loss. 10 relabelled 6% of days
                per weekly refit on real data with bull/sideways/bear shares
                of 49/32/19%; 50 relabelled 3% but left "sideways" at 11%.
            max_iter: Max coordinate-descent iterations per initialisation.
            random_state: Seed for centroid initialisation reproducibility.
            return_col: Returns column; the label ranking without ``vol_col``.
            n_init: Number of k-means++ restarts; the lowest objective wins.
            vol_col: Feature the states are ranked by for labelling.
        """
        if n_states != 3:
            raise ValueError(
                f"JumpRegimeModel requires n_states=3 for deterministic labelling; got {n_states}"
            )

        self.n_states = n_states
        self.jump_penalty = jump_penalty
        self.max_iter = max_iter
        self.random_state = random_state
        self.return_col = return_col
        self.n_init = n_init
        self.vol_col = vol_col

        self.feature_columns: list[str] | None = None
        self.scaler: StandardScaler | None = None
        self.model: Any | None = None
        self._state_to_label: dict[int, str] | None = None
        self._converged: bool = False
        self._reliability: np.ndarray | None = None

    def _jump_model_cls(self) -> Any:
        """Resolve the upstream class or raise the actionable RuntimeError."""
        try:
            return _import_jump_model()
        except ImportError as exc:
            raise RuntimeError(_INSTALL_HINT) from exc

    @property
    def converged(self) -> bool:
        """Whether the most recent fit() completed its descent loop.

        False before any fit() call. The upstream implementation stops when
        labels stabilise or tol/max_iter is reached and exposes no separate
        convergence monitor, so unlike RegimeHMM there are no retries here.
        """
        return self._converged

    def fit(self, X: pd.DataFrame) -> JumpRegimeModel:
        """Fit the jump model to features.

        Args:
            X: DataFrame with features (columns become self.feature_columns).
                Must include return_col.

        Returns:
            self for chaining.

        Raises:
            ValueError: if return_col is not in X.columns.
            RuntimeError: if the jumpmodels package is absent.
        """
        if self.return_col not in X.columns:
            raise ValueError(
                f"Feature column '{self.return_col}' not found in X.columns: {list(X.columns)}"
            )

        JumpModel = self._jump_model_cls()

        self.feature_columns = list(X.columns)
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        model = JumpModel(
            n_components=self.n_states,
            jump_penalty=self.jump_penalty,
            max_iter=self.max_iter,
            random_state=self.random_state,
            n_init=self.n_init,
        )
        model.fit(np.asarray(X_scaled))
        self._converged = True

        # Rank the FINAL centres, which is invariant to any internal state
        # permutation upstream applies.
        centers = np.asarray(model.centers_)
        if self.vol_col in self.feature_columns:
            ranked = np.argsort(centers[:, self.feature_columns.index(self.vol_col)])
            self._state_to_label = {
                int(ranked[0]): "bull",
                int(ranked[1]): "sideways",
                int(ranked[2]): "bear",
            }
        else:
            ranked = np.argsort(centers[:, self.feature_columns.index(self.return_col)])
            self._state_to_label = {
                int(ranked[2]): "bull",
                int(ranked[1]): "sideways",
                int(ranked[0]): "bear",
            }
        self.model = model
        self._reliability = self._reliability_table(np.asarray(X_scaled))
        return self

    @staticmethod
    def _run_bucket(run_length: int) -> int:
        return sum(run_length >= edge for edge in _RUN_BUCKETS)

    @staticmethod
    def _run_lengths(labels: np.ndarray) -> np.ndarray:
        runs = np.zeros(len(labels), dtype=int)
        for i in range(1, len(labels)):
            runs[i] = runs[i - 1] + 1 if labels[i] == labels[i - 1] else 0
        return runs

    def _reliability_table(self, X_scaled: np.ndarray) -> np.ndarray:
        """P(hindsight label | online label, run-length bucket) over the fit window.

        The live label is decoded causally (:meth:`predict_online`), and the
        same model decoding the whole window in hindsight revises it on some
        days, mostly at the end of a bear phase. This table counts, per
        bucket of days since the online label last changed, how often each
        online label kept or changed its label in hindsight (Laplace +1).

        Replayed on real data (803 live days, weekly refits 2023-06..2026-06,
        truth = the same model's decode with 63 more days), these
        probabilities scored Brier 0.078 and log loss 0.170. The DP softmax
        the score used to be scored 0.093 / 0.305 with a mean of 0.998 (days
        it put at 0.95-0.99 were right 36% of the time), and the continuous
        jump model (Aydinhan et al.) scored 0.16-0.26 / 1.0-1.7 at penalties
        10-100.

        Returns:
            Array ``[bucket, online_label, hindsight_label]`` of probabilities,
            labels in :data:`LABELS` order.
        """
        assert self.model is not None and self._state_to_label is not None
        index = {label: i for i, label in enumerate(LABELS)}
        to_idx = np.vectorize(lambda s: index[self._state_to_label[int(s)]])  # type: ignore[index]
        online = to_idx(np.asarray(self.model.predict_online(X_scaled)))
        hindsight = to_idx(np.asarray(self.model.predict(X_scaled)))
        counts = np.ones((len(_RUN_BUCKETS) + 1, len(LABELS), len(LABELS)))
        for label, truth, run in zip(online, hindsight, self._run_lengths(online)):
            counts[self._run_bucket(int(run)), label, truth] += 1
        return counts / counts.sum(axis=2, keepdims=True)

    def predict(self, X: pd.DataFrame) -> list[str]:
        """Regime label per row, decoded over the whole frame (in-sample view).

        Raises:
            ValueError: if required columns are missing or model unfit.
        """
        if self.model is None or self.scaler is None or self._state_to_label is None:
            raise ValueError("Model must be fit before predict")

        X_scaled = self.scaler.transform(self._reindex_features(X))
        states = np.asarray(self.model.predict(np.asarray(X_scaled)))
        return [self._state_to_label[int(s)] for s in states]

    def predict_online(self, X: pd.DataFrame) -> list[str]:
        """Regime label per row using only rows up to it (what a live day sees)."""
        if self.model is None or self.scaler is None or self._state_to_label is None:
            raise ValueError("Model must be fit before predict_online")

        X_scaled = self.scaler.transform(self._reindex_features(X))
        states = np.asarray(self.model.predict_online(np.asarray(X_scaled)))
        return [self._state_to_label[int(s)] for s in states]

    def predict_state_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Per-row state scores from the causal dynamic-programming pass.

        The discrete jump model's own probabilities are one-hot, which would
        report every day with 100% confidence. Its loss is the negative log
        likelihood of unit-variance Gaussian emissions and its penalty a
        negative log transition probability (Bemporad et al. 2018), so
        ``softmax(-V_t)`` over the online DP values ``V_t`` — the least total
        cost of ending in each state at row t — is the max-product analogue
        of a filtered posterior. Its argmax is :meth:`predict_online`.

        Returns:
            Array of shape (n_rows, n_states).
        """
        if self.model is None or self.scaler is None:
            raise ValueError("Model must be fit before predict_state_proba")

        from jumpmodels.jump import do_E_step

        X_scaled = np.asarray(self.scaler.transform(self._reindex_features(X)))
        value_mx = np.asarray(
            do_E_step(
                X_scaled,
                self.model.centers_,
                self.model.jump_penalty_mx,
                self.model.prob_vecs,
                return_value_mx=True,
            ),
            dtype=float,
        )
        shifted = -(value_mx - value_mx.min(axis=1, keepdims=True))
        weights = np.exp(shifted)
        return weights / weights.sum(axis=1, keepdims=True)

    def classify_latest(self, X: pd.DataFrame) -> dict[str, Any]:
        """Classify the latest row causally and say how far to trust the label.

        ``probs`` is the row of the fitted reliability table for today's
        online label and its run length: the probability of each label
        being the one the model assigns once more data arrives. ``score`` is
        the current label's entry. A model fitted before the table existed
        falls back to :meth:`predict_state_proba`, which is close to 1 on
        almost every day.

        Returns:
            Dict with ``label``, ``score``, ``probs`` per label and
            ``score_basis`` ("label_reliability" or "dp_softmax").

        Raises:
            ValueError: if X is empty or model is not fit.
        """
        if self.model is None or self.scaler is None or self._state_to_label is None:
            raise ValueError("Model must be fit before classify_latest")

        if len(X) == 0:
            raise ValueError("X must not be empty")

        proba = self.predict_state_proba(X)
        states = np.argmax(proba, axis=1)
        label = self._state_to_label[int(states[-1])]

        if self._reliability is not None:
            online = np.array([self._state_to_label[int(s)] for s in states])
            run = int(self._run_lengths(online)[-1])
            row = self._reliability[self._run_bucket(run), LABELS.index(label)]
            probs = {name: float(row[i]) for i, name in enumerate(LABELS)}
            return {
                "label": label,
                "score": probs[label],
                "probs": probs,
                "score_basis": "label_reliability",
                "run_length": run,
            }

        last = proba[-1, :]
        probs = {"bull": 0.0, "sideways": 0.0, "bear": 0.0}
        for state_idx, state_label in self._state_to_label.items():
            probs[state_label] += float(last[state_idx])
        return {"label": label, "score": probs[label], "probs": probs, "score_basis": "dp_softmax"}

    def save(self, model_id: str, base: str | None = None) -> str:
        """Persist a plain state dict (see RegimeHMM.save for why not ``self``).

        Only the upstream JumpModel, the scaler and plain data are stored, so
        moving this class never breaks a persisted artefact.

        Raises:
            ValueError: if model is not fit.
        """
        if self.model is None or self.scaler is None or self._state_to_label is None:
            raise ValueError("Model must be fit before save")

        state = {
            "schema_version": _ARTIFACT_SCHEMA_VERSION,
            "kind": "jump",
            "n_states": self.n_states,
            "jump_penalty": self.jump_penalty,
            "max_iter": self.max_iter,
            "random_state": self.random_state,
            "return_col": self.return_col,
            "n_init": self.n_init,
            "vol_col": self.vol_col,
            "feature_columns": self.feature_columns,
            "scaler": self.scaler,
            "model": self.model,
            "state_to_label": self._state_to_label,
            "converged": self._converged,
            "reliability": self._reliability,
        }
        kwargs = {"model_id": model_id}
        if base is not None:
            kwargs["base"] = base
        return save_model(state, **kwargs)

    @classmethod
    def load(cls, model_id: str, base: str | None = None) -> JumpRegimeModel:
        """Load a model saved by :meth:`save`.

        Raises:
            ValueError: if the artefact is missing or not a current state dict.
        """
        kwargs = {"model_id": model_id}
        if base is not None:
            kwargs["base"] = base

        state = _load_model_unvalidated(**kwargs)
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != _ARTIFACT_SCHEMA_VERSION
            or state.get("kind") != "jump"
        ):
            raise ValueError(
                f"Unsupported JumpRegimeModel artefact for model_id={model_id!r}; "
                "refit and re-save under the current code before loading it again"
            )

        instance = cls(
            n_states=state["n_states"],
            jump_penalty=state["jump_penalty"],
            max_iter=state["max_iter"],
            random_state=state["random_state"],
            return_col=state["return_col"],
            n_init=state["n_init"],
            vol_col=state["vol_col"],
        )
        instance.feature_columns = state["feature_columns"]
        instance.scaler = state["scaler"]
        instance.model = state["model"]
        instance._state_to_label = state["state_to_label"]
        instance._converged = state["converged"]
        reliability = state.get("reliability")
        instance._reliability = np.asarray(reliability) if reliability is not None else None
        return instance

    def _reindex_features(self, X: pd.DataFrame) -> pd.DataFrame:
        """Reindex X to the fitted feature columns.

        Raises:
            ValueError: if any required column is missing.
        """
        if self.feature_columns is None:
            raise ValueError("Model must be fit before _reindex_features")

        missing = set(self.feature_columns) - set(X.columns)
        if missing:
            raise ValueError(
                f"Missing required columns: {missing}. Expected: {self.feature_columns}"
            )

        return cast(pd.DataFrame, X[self.feature_columns])
