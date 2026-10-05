"""Discover pipeline state machine types."""
from __future__ import annotations

from enum import Enum
from typing import Any


class DiscoveryState(str, Enum):
    IDLE = "idle"
    BUILDING_UNIVERSE = "building_universe"
    PIPELINE_CANDIDATES = "pipeline_candidates"
    ASSESSING_TRADEABILITY = "assessing_tradeability"
    WRITING_DOSSIER = "writing_dossier"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CANCELLATION_REQUESTED = "cancellation_requested"


def make_stage_json(
    state: DiscoveryState,
    *,
    total_candidates: int | None = None,
    processed_candidates: int | None = None,
    current_candidate: str | None = None,
    current_stage: str | None = None,
    elapsed_seconds: float | None = None,
    warmup_done: int | None = None,
    warmup_total: int | None = None,
    benchmark_ingest: dict[str, Any] | None = None,
    message: str = "",
) -> dict:
    """Build a structured stage_json dict for frontend progress display."""
    d: dict = {"state": state.value, "message": message}
    if total_candidates is not None:
        d["total_candidates"] = total_candidates
    if processed_candidates is not None:
        d["processed_candidates"] = processed_candidates
    if current_candidate is not None:
        d["current_candidate"] = current_candidate
    if current_stage is not None:
        d["current_stage"] = current_stage
    if elapsed_seconds is not None:
        d["elapsed_seconds"] = elapsed_seconds
    # Warm-up (price-cache) progress so the UI can show a determinate bar during
    # the opaque pre-ingest phase instead of sitting pinned at 0%.
    if warmup_done is not None:
        d["warmup_done"] = warmup_done
    if warmup_total is not None:
        d["warmup_total"] = warmup_total
    # Warm-up outcome ({requested, ok, failed}) incl. the benchmark ETFs, so a
    # degraded benchmark ingest is visible on the run record instead of silent.
    if benchmark_ingest is not None:
        d["benchmark_ingest"] = benchmark_ingest
    return d
