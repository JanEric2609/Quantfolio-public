"""Regime label hysteresis across refits (plan 2026-10-12, D9 / WP7).

On Sunday 2026-10-11 the weekly refit turned "bull 0.976" into "sideways
0.978" with no new bar since Friday; sideways switches on
``momentum_suppressed`` and ``position_scale 0.8`` (``gate.py``), so the refit
alone changed decisions. Rules, applied to the label the model gives today:

* the same as the published label: publish it, clear any pending change;
* different, but no new feature row since the last published snapshot:
  keep the published label (``label_source = "carried_no_new_data"``);
* different on new data, with no refit since the last snapshot and no
  change pending: publish it (an ordinary regime change);
* different on new data after a refit (or while a change is pending): keep
  the published label (``"carried_after_refit"``) until 2 consecutive daily
  runs on new data agree on the new label.

Pure: the caller passes the previous snapshot's label and payload.
"""
from __future__ import annotations

from typing import Any

#: Consecutive agreeing runs on new data before a post-refit change is published.
CONFIRM_RUNS = 2


def apply_hysteresis(
    model_label: str,
    *,
    previous_label: str | None,
    previous_payload: dict[str, Any] | None,
    feature_ts: str,
    fingerprint: str | None,
) -> tuple[str, dict[str, Any]]:
    """``(published label, payload fields)`` for today's run."""
    prev = previous_payload or {}
    fields: dict[str, Any] = {
        "model_label": model_label,
        "model_fingerprint": fingerprint,
        "label_source": "model",
        "pending_label": None,
        "pending_count": 0,
    }
    if previous_label is None or model_label == previous_label:
        return model_label, fields

    prev_feature_ts = str(prev.get("feature_ts") or "")
    new_data = not prev_feature_ts or feature_ts > prev_feature_ts
    if not new_data:
        # Keep whatever change was pending, but a day without data never counts.
        fields.update({
            "label_source": "carried_no_new_data",
            "pending_label": prev.get("pending_label"),
            "pending_count": int(prev.get("pending_count") or 0),
        })
        return previous_label, fields

    prev_fp = prev.get("model_fingerprint")
    refit = fingerprint is not None and prev_fp is not None and fingerprint != prev_fp
    pending = prev.get("pending_label")
    if not refit and not pending:
        return model_label, fields

    count = int(prev.get("pending_count") or 0) + 1 if pending == model_label else 1
    if count >= CONFIRM_RUNS:
        fields["label_source"] = "confirmed_after_refit"
        return model_label, fields
    fields.update({"label_source": "carried_after_refit", "pending_label": model_label, "pending_count": count})
    return previous_label, fields
