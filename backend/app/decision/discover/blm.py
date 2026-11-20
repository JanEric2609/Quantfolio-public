"""Black-Litterman model for stabilizing LLM-generated return views.

Implements the canonical Black-Litterman posterior formula from
Lee et al. (arXiv:2504.14345). This module has no FastAPI, database, or
SQLAlchemy dependencies so it can be used from any backend service or agent.

Workflow:
    1. Compute market-implied equilibrium returns: ``Π = δ Σ w_mkt``.
    2. Collect LLM return expectations into views ``Q`` and pick matrix ``P``.
    3. Estimate view confidence as a diagonal variance matrix ``Ω``.
    4. Combine prior and views to obtain posterior returns/covariance.

References:
    Lee et al. Black-Litterman model for LLM-generated views.
    arXiv:2504.14345, 2025.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from app.foundation.settings import llm_auth_headers

logger = logging.getLogger(__name__)

__all__ = [
    "BLMError",
    "BLMResult",
    "BL_TAU_DEFAULT",
    "compute_equilibrium_returns",
    "blm_posterior",
    "blm_posterior_proportional",
    "multi_query_confidence",
    "multi_query_confidence_batch",
]


# ---------------------------------------------------------------------------
# Exceptions and models
# ---------------------------------------------------------------------------


class BLMError(ValueError):
    """Raised when BLM computation fails (singular matrix, invalid views, etc.)."""

    pass


class BLMResult(BaseModel):
    """Serializable container for Black-Litterman computation outputs."""

    model_config = ConfigDict(frozen=True)

    portfolio_id: str
    asset_ids: list[str] = Field(default_factory=list)
    prior_returns: list[float] = Field(default_factory=list)
    posterior_returns: list[float] = Field(default_factory=list)
    posterior_cov: list[list[float]] = Field(default_factory=list)
    views: dict[str, dict[str, float]] = Field(default_factory=dict)
    tau: float = 0.025
    risk_aversion: float = 2.5


# ---------------------------------------------------------------------------
# Core Black-Litterman math
# ---------------------------------------------------------------------------

# THE single tau source for every Black-Litterman surface in the codebase
# (M5 Option 2 single-tau-source rule): blm_posterior's default imports this
# constant — the historical 0.025-vs-0.05 drift must not reopen, and the
# deleted quant_optim duplicate (ADR 0003 decision 3) may not return.
BL_TAU_DEFAULT: float = 0.025


def compute_equilibrium_returns(
    cov_matrix: NDArray[np.float64],
    market_weights: NDArray[np.float64],
    risk_aversion: float = 2.5,
) -> NDArray[np.float64]:
    """Compute market-implied equilibrium returns via reverse optimization.

    ``Π = δ Σ w_mkt`` where ``δ`` is the risk-aversion coefficient.

    Args:
        cov_matrix: ``n × n`` covariance matrix.
        market_weights: ``n``-length market weights.
        risk_aversion: Aggregate risk-aversion ``δ``.

    Returns:
        ``n``-length equilibrium return vector.

    Raises:
        BLMError: If inputs have incompatible dimensions or ``risk_aversion <= 0``.
    """
    cov = np.asarray(cov_matrix, dtype=np.float64)
    weights = np.asarray(market_weights, dtype=np.float64)

    if cov.ndim != 2 or cov.shape[0] != cov.shape[1]:
        raise BLMError("cov_matrix must be a square 2-D array")
    if weights.ndim != 1:
        raise BLMError("market_weights must be a 1-D array")
    if cov.shape[0] != weights.shape[0]:
        raise BLMError("cov_matrix and market_weights dimensions do not match")
    if risk_aversion <= 0:
        raise BLMError("risk_aversion must be positive")

    return risk_aversion * (cov @ weights)


def blm_posterior(
    prior_returns: NDArray[np.float64],
    prior_cov: NDArray[np.float64],
    views: dict[int, float],
    view_variances: dict[int, float],
    tau: float = BL_TAU_DEFAULT,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Compute Black-Litterman posterior returns and covariance.

    Formulas:
        E(R) = [(τΣ)^-1 + P^T Ω^-1 P]^-1 [(τΣ)^-1 Π + P^T Ω^-1 Q]
        Σ_post = Σ + [(τΣ)^-1 + P^T Ω^-1 P]^-1

    Args:
        prior_returns: ``n``-length prior return vector ``Π``.
        prior_cov: ``n × n`` covariance matrix ``Σ``.
        views: ``{asset_index: expected_return}`` for the ``Q`` vector.
        view_variances: ``{asset_index: variance}`` for the diagonal of ``Ω``.
        tau: Prior uncertainty scaling parameter.

    Returns:
        ``(posterior_returns, posterior_cov)`` with shapes ``(n,)`` and ``(n, n)``.

    Raises:
        BLMError: If the covariance matrix is singular, view indices are invalid,
            or any view variance is zero/negative.
    """
    prior_returns_arr = np.asarray(prior_returns, dtype=np.float64)
    prior_cov_arr = np.asarray(prior_cov, dtype=np.float64)

    if prior_returns_arr.ndim != 1:
        raise BLMError("prior_returns must be a 1-D array")
    if prior_cov_arr.ndim != 2 or prior_cov_arr.shape[0] != prior_cov_arr.shape[1]:
        raise BLMError("prior_cov must be a square 2-D array")

    n = prior_returns_arr.shape[0]
    if prior_cov_arr.shape[0] != n:
        raise BLMError("prior_returns and prior_cov dimensions do not match")
    if tau <= 0:
        raise BLMError("tau must be positive")

    if len(views) == 0:
        return prior_returns_arr.copy(), prior_cov_arr.copy()

    for idx in views:
        if idx not in view_variances:
            raise BLMError(f"view variance missing for asset index {idx}")
    for idx in view_variances:
        if idx not in views:
            raise BLMError(f"view return missing for asset index {idx}")

    sorted_indices = sorted(views.keys())
    k = len(sorted_indices)

    P = np.zeros((k, n), dtype=np.float64)
    Q = np.zeros(k, dtype=np.float64)
    omega_diag = np.zeros(k, dtype=np.float64)

    MIN_VIEW_VARIANCE = 1e-8

    for i, asset_idx in enumerate(sorted_indices):
        if not (0 <= asset_idx < n):
            raise BLMError(f"view asset index {asset_idx} out of range [0, {n})")

        P[i, asset_idx] = 1.0
        Q[i] = float(views[asset_idx])

        var = float(view_variances[asset_idx])
        if var <= 0:
            raise BLMError(
                f"view variance for asset index {asset_idx} must be positive "
                f"(zero implies infinite confidence; use at least {MIN_VIEW_VARIANCE:g})"
            )
        if var < MIN_VIEW_VARIANCE:
            logger.warning(
                "view variance for asset index %d clamped to %.0e", asset_idx, MIN_VIEW_VARIANCE
            )
            var = MIN_VIEW_VARIANCE
        omega_diag[i] = var

    omega = np.diag(omega_diag)

    cond = np.linalg.cond(prior_cov_arr)
    if not np.isfinite(cond) or cond > 1e12:
        raise BLMError(f"prior_cov is singular or near-singular (condition number = {cond:.2e})")

    try:
        tau_sigma_inv = np.linalg.inv(tau * prior_cov_arr)
        omega_inv = np.linalg.inv(omega)
        M = np.linalg.inv(tau_sigma_inv + P.T @ omega_inv @ P)
    except np.linalg.LinAlgError as exc:
        raise BLMError(f"matrix inversion failed: {exc}") from exc

    posterior_returns = M @ (tau_sigma_inv @ prior_returns_arr + P.T @ omega_inv @ Q)
    posterior_cov = prior_cov_arr + M

    return posterior_returns, posterior_cov


def blm_posterior_proportional(
    prior_returns: NDArray[np.float64],
    prior_cov: NDArray[np.float64],
    views: dict[int, float],
    view_confidences: dict[int, float],
    tau: float | None = None,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Black-Litterman posterior with proportional Omega: ``Ω = k·τ·PΣPᵀ``.

    M5 Option 2 (discover-maths audit F14), Oracle-corrected construction.
    The confidence→k mapping is the proportional-Omega heuristic
    ``k = (1 − conf) + 0.01`` floor (mirrors quant_optim's historical
    ad-hoc scaling) — it is NOT Idzorek and must never be described as such
    (no confidence-interval matching, no per-view weight optimization).

    With ALL views sharing ONE k, ``Ω + τPΣPᵀ = (1+k)·τ·PΣPᵀ``, so the
    standard posterior mean collapses to the closed form

        μ_post = Π + (1/(1+k)) · ΣPᵀ(PΣPᵀ)⁻¹ (Q − P·Π)

    in which τ cancels EXACTLY — sweeping tau over [0.01, 0.10] leaves the
    mean bit-for-bit stable. A per-view k diagonal would break that exact
    cancellation (only approximate invariance), which is why views are pinned
    to a single shared k = mean(confidence-derived k) by design.

    Posterior covariance follows the uncertainty-shrinkage convention
    ``Σ_post = Σ − (τ/(1+k))·ΣPᵀ(PΣPᵀ)⁻¹PΣ``, symmetrized against
    floating-point drift.

    Args:
        prior_returns: ``n``-length equilibrium prior ``Π``
            (:func:`compute_equilibrium_returns`).
        prior_cov: ``n × n`` annualized covariance ``Σ``.
        views: ``{asset_index: expected_return}`` absolute views (Q).
        view_confidences: ``{asset_index: confidence}`` in [0, 1]; k derives
            from their mean.
        tau: Prior uncertainty scaling; defaults to :data:`BL_TAU_DEFAULT`.

    Returns:
        ``(posterior_returns, posterior_cov)`` with shapes ``(n,)`` and
        ``(n, n)``. Empty ``views`` returns copies of the prior untouched.

    Raises:
        BLMError: On dimension mismatches, non-positive tau, or a singular /
        near-singular ``PΣPᵀ`` (collinear views — e.g. duplicated assets make
        identical P rows). Raising matches this module's existing convention
        (:func:`blm_posterior`); callers fail open to their fallback anchor.
    """
    pi = np.asarray(prior_returns, dtype=np.float64)
    sigma = np.asarray(prior_cov, dtype=np.float64)

    if pi.ndim != 1:
        raise BLMError("prior_returns must be a 1-D array")
    if sigma.ndim != 2 or sigma.shape[0] != sigma.shape[1]:
        raise BLMError("prior_cov must be a square 2-D array")
    if sigma.shape[0] != pi.shape[0]:
        raise BLMError("prior_returns and prior_cov dimensions do not match")
    if not views:
        return pi.copy(), sigma.copy()

    resolved_tau = BL_TAU_DEFAULT if tau is None else float(tau)
    if resolved_tau <= 0:
        raise BLMError("tau must be positive")

    for idx in views:
        if idx not in view_confidences:
            raise BLMError(f"view confidence missing for asset index {idx}")
    for idx in view_confidences:
        if idx not in views:
            raise BLMError(f"view return missing for asset index {idx}")

    n = pi.shape[0]
    sorted_indices = sorted(views.keys())
    k_views = len(sorted_indices)

    p_mat = np.zeros((k_views, n), dtype=np.float64)
    q_vec = np.zeros(k_views, dtype=np.float64)
    ks: list[float] = []
    for i, asset_idx in enumerate(sorted_indices):
        if not (0 <= asset_idx < n):
            raise BLMError(f"view asset index {asset_idx} out of range [0, {n})")
        p_mat[i, asset_idx] = 1.0
        q_vec[i] = float(views[asset_idx])
        conf = min(1.0, max(0.0, float(view_confidences[asset_idx])))
        ks.append((1.0 - conf) + 0.01)

    # Single shared k: exact tau cancellation only holds when every view
    # scales Omega by the same factor (see docstring).
    k_shared = float(np.mean(ks))

    p_sigma_pt = p_mat @ sigma @ p_mat.T
    cond = np.linalg.cond(p_sigma_pt)
    if not np.isfinite(cond) or cond > 1e12:
        raise BLMError(f"views are collinear or cov is near-singular (cond(PΣPᵀ) = {cond:.2e})")

    try:
        c_inv = np.linalg.inv(p_sigma_pt)
    except np.linalg.LinAlgError as exc:
        raise BLMError(f"matrix inversion failed: {exc}") from exc

    sigma_pt = sigma @ p_mat.T
    mu_posterior = pi + (1.0 / (1.0 + k_shared)) * (sigma_pt @ c_inv @ (q_vec - p_mat @ pi))
    posterior_cov = sigma - (resolved_tau / (1.0 + k_shared)) * (sigma_pt @ c_inv @ p_mat @ sigma)
    posterior_cov = (posterior_cov + posterior_cov.T) / 2.0

    return mu_posterior, posterior_cov


# ---------------------------------------------------------------------------
# LLM multi-query confidence estimation
# ---------------------------------------------------------------------------


def _extract_return_value(text: str) -> float | None:
    """Extract the first numeric value from an LLM response.

    Percentages are normalized to decimals (``5%`` → ``0.05``).
    """
    if not text:
        return None

    match = re.search(r"([-+]?\d*\.?\d+)\s*%", text)
    is_percentage = bool(match)
    if not match:
        match = re.search(r"([-+]?\d*\.?\d+)", text)
    if not match:
        return None

    try:
        value = float(match.group(1))
    except ValueError:
        return None

    if is_percentage:
        value = value / 100.0

    return value


def _build_llm_payload(symbol: str, model: str) -> dict[str, Any]:
    """Build the JSON payload for a single LLM query.

    Reasoning is disabled via ``reasoning_effort: none`` as a Machine Spirits
    mitigation.
    """
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"What is the expected 2-week return for {symbol}? Answer with a single number."
                ),
            }
        ],
        "temperature": 0.2,
        "max_tokens": 32,
        "reasoning_effort": "none",
    }


async def _query_llm_single(
    symbol: str,
    llm_endpoint: str,
    llm_model: str,
    timeout: float,
    client: httpx.AsyncClient,
) -> float | None:
    """Execute one LLM query and parse a single return value.

    Failures are logged and returned as ``None`` so the caller can aggregate
    successful responses.
    """
    payload = _build_llm_payload(symbol, llm_model)
    url = f"{llm_endpoint.rstrip('/')}/chat/completions"

    # No db session here, so only LLM_API_KEY from the environment applies.
    auth = llm_auth_headers()
    auth_kwargs: dict[str, Any] = {"headers": auth} if auth else {}
    try:
        response = await client.post(url, json=payload, timeout=timeout, **auth_kwargs)
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException:
        logger.warning("LLM query timed out for %s", symbol)
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning("LLM query failed for %s: %s", symbol, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM query transport error for %s: %s", symbol, exc)
        return None

    choices = data.get("choices") or []
    if not choices:
        logger.warning("empty choices in LLM response for %s", symbol)
        return None
    content = choices[0].get("message", {}).get("content", "")
    value = _extract_return_value(content)
    if value is None:
        logger.warning("could not parse return value from LLM for %s: %r", symbol, content)
        return None

    return value


async def multi_query_confidence(
    asset_symbol: str,
    n_queries: int = 25,
    llm_endpoint: str = "",
    llm_model: str = "qwen3.5-9b",
    timeout: float = 30.0,
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[float, float] | None:
    """Query an LLM ``n_queries`` times for a 2-week return estimate.

    Queries are gathered concurrently but staggered by 100 ms to avoid
    overwhelming a local llama.cpp server. Individual failures are dropped
    and the remaining successful responses are used to compute mean/variance.

    Args:
        asset_symbol: Ticker or asset identifier.
        n_queries: Number of independent queries (default 25 — sized for a
            shared single-GPU deployment; each query is a full chat-completion
            call against the same local llama.cpp instance that also serves
            mandate reviews and the advisor loop).
        llm_endpoint: Base URL of the llama.cpp server.
        llm_model: Model name accepted by the server.
        timeout: Per-request timeout in seconds.

    Returns:
        ``(mean_return, variance)`` with returns clamped to ``[-1.0, 1.0]``.
        If no query succeeds, returns ``None`` — ADR 0014 §5: a prior
        ``(0.0, 1.0)`` sentinel was indistinguishable from a genuine
        near-zero forecast with high variance, so callers could not tell
        "all queries failed" from "the model thinks returns are flat."
    """
    if n_queries <= 0:
        raise BLMError("n_queries must be positive")
    if not llm_endpoint:
        raise BLMError("llm_endpoint is required but not configured")

    async def _staggered_query(index: int, client: httpx.AsyncClient) -> float | None:
        await asyncio.sleep(index * 0.1)
        if semaphore:
            async with semaphore:
                return await _query_llm_single(
                    asset_symbol, llm_endpoint, llm_model, timeout, client
                )
        return await _query_llm_single(asset_symbol, llm_endpoint, llm_model, timeout, client)

    async with httpx.AsyncClient() as client:
        tasks = [_staggered_query(i, client) for i in range(n_queries)]
        results = await asyncio.gather(*tasks)

    values = [value for value in results if value is not None]

    if not values:
        logger.warning(
            "all %d LLM queries failed for %s; no usable view",
            n_queries,
            asset_symbol,
        )
        return None

    if len(values) < n_queries:
        logger.warning(
            "only %d/%d LLM queries succeeded for %s", len(values), n_queries, asset_symbol
        )

    clipped = np.clip(values, -1.0, 1.0)
    arr = np.asarray(clipped, dtype=np.float64)

    mean = float(np.mean(arr))
    variance = float(np.var(arr, ddof=0))
    return mean, max(variance, 1e-8)


async def multi_query_confidence_batch(
    assets: list[str],
    n_queries: int = 25,
    llm_endpoint: str = "",
    llm_model: str = "qwen3.5-9b",
    max_concurrent: int = 5,
) -> dict[str, tuple[float, float] | None]:
    """Run ``multi_query_confidence`` for multiple assets concurrently."""
    if not assets:
        return {}
    if not llm_endpoint:
        raise BLMError("llm_endpoint is required but not configured")

    sem = asyncio.Semaphore(max_concurrent)

    tasks = [
        multi_query_confidence(
            asset_symbol=asset,
            n_queries=n_queries,
            llm_endpoint=llm_endpoint,
            llm_model=llm_model,
            semaphore=sem,
        )
        for asset in assets
    ]
    results = await asyncio.gather(*tasks)
    return {asset: result for asset, result in zip(assets, results)}
