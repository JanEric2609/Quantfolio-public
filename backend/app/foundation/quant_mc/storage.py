"""Storage of MC path samples to database.

Downsamples large path matrices before storage to keep hypertable manageable.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional
from uuid import UUID

import numpy as np


@dataclass
class PathStorageSpec:
    """Configuration for path storage."""

    run_id: UUID
    n_downsample: int = 100  # Max paths to store
    n_steps_downsample: int = 200  # Max steps per path


def downsample_paths(
    paths: np.ndarray,
    n_paths_target: int = 100,
    n_steps_target: int = 200,
) -> np.ndarray:
    """Downsample paths for storage.

    Args:
        paths: (n_paths, n_steps) array
        n_paths_target: Target number of paths
        n_steps_target: Target number of steps per path

    Returns:
        (n_paths_downsampled, n_steps_downsampled) array
    """
    n_paths, n_steps = paths.shape

    # Downsample paths
    if n_paths > n_paths_target:
        indices_paths = np.linspace(0, n_paths - 1, n_paths_target, dtype=int)
        paths = paths[indices_paths, :]
    else:
        indices_paths = np.arange(n_paths)

    # Downsample time steps
    if n_steps > n_steps_target:
        indices_steps = np.linspace(0, n_steps - 1, n_steps_target, dtype=int)
        paths = paths[:, indices_steps]
    else:
        indices_steps = np.arange(n_steps)

    return paths


def format_paths_for_storage(
    run_id: UUID,
    paths: np.ndarray,
    downsampled_paths: np.ndarray,
    spec: Optional[PathStorageSpec] = None,
) -> list[dict]:
    """Format paths as a list of records for mc_path_samples table.

    Each record: {run_id, path_id, ts, value}
    ts is synthetic (0, 1, 2, ..., n_steps-1)

    Returns list of dicts ready for bulk insert.
    """
    if spec is None:
        spec = PathStorageSpec(run_id=run_id)

    n_paths, n_steps = downsampled_paths.shape
    records = []
    base_ts = datetime.now(UTC)

    for path_id in range(n_paths):
        for step_id in range(n_steps):
            records.append({
                "run_id": str(run_id),
                "path_id": int(path_id),
                # Assign a distinct timestamp per step so hypertable rows are
                # uniquely ordered; step_id=0 gets base_ts, each subsequent step
                # is offset by one hour.
                "ts": (base_ts + timedelta(hours=step_id)).isoformat(),
                "value": float(downsampled_paths[path_id, step_id]),
            })

    return records


def compute_path_summary(paths: np.ndarray, quantiles: list[float] | None = None) -> dict:
    """Compute summary statistics of paths for storage in paths_summary_json.

    Args:
        paths: (n_paths, n_steps) array
        quantiles: List of quantiles to compute (default: [0.05, 0.25, 0.5, 0.75, 0.95])

    Returns:
        Summary dict with percentiles and basic stats.
    """
    if quantiles is None:
        quantiles = [0.05, 0.25, 0.5, 0.75, 0.95]

    final_values = paths[:, -1]
    summary = {
        "n_paths": int(paths.shape[0]),
        "n_steps": int(paths.shape[1]),
        "mean": float(np.mean(final_values)),
        "std": float(np.std(final_values)),
        "min": float(np.min(final_values)),
        "max": float(np.max(final_values)),
        "percentiles": {},
    }

    for q in quantiles:
        summary["percentiles"][str(int(q * 100))] = float(np.percentile(final_values, q * 100))

    # Path envelope: p5, median, p95 at each step
    p5 = np.percentile(paths, 5, axis=0)
    p50 = np.percentile(paths, 50, axis=0)
    p95 = np.percentile(paths, 95, axis=0)

    summary["envelope"] = {
        "p5": [float(x) for x in p5],
        "p50": [float(x) for x in p50],
        "p95": [float(x) for x in p95],
    }

    return summary
