"""Regime label hysteresis across refits (plan 2026-10-12, D9 / WP7)."""
from __future__ import annotations

from app.lab.regime.hysteresis import apply_hysteresis

FRI = "2026-10-09T00:00:00+00:00"
MON = "2026-10-12T00:00:00+00:00"
TUE = "2026-10-13T00:00:00+00:00"
WED = "2026-10-14T00:00:00+00:00"


def _prev(label="bull", feature_ts=FRI, fp="old", **extra):
    return {"previous_label": label, "previous_payload": {"feature_ts": feature_ts, "model_fingerprint": fp, **extra}}


def test_a_refit_with_no_new_data_never_changes_the_label():
    # Sunday 2026-10-11: refit, features still Friday's.
    label, f = apply_hysteresis("sideways", feature_ts=FRI, fingerprint="new", **_prev())
    assert label == "bull"
    assert f["label_source"] == "carried_no_new_data"
    assert f["model_label"] == "sideways"


def test_two_agreeing_new_days_after_a_refit_switch_the_label():
    label, f1 = apply_hysteresis("sideways", feature_ts=MON, fingerprint="new", **_prev())
    assert label == "bull" and f1["label_source"] == "carried_after_refit" and f1["pending_count"] == 1
    payload = {"feature_ts": MON, "model_fingerprint": "new", "pending_label": "sideways", "pending_count": 1}
    label, f2 = apply_hysteresis("sideways", feature_ts=TUE, fingerprint="new",
                                 previous_label="bull", previous_payload=payload)
    assert label == "sideways" and f2["label_source"] == "confirmed_after_refit" and f2["pending_label"] is None


def test_a_disagreeing_day_restarts_the_count():
    payload = {"feature_ts": MON, "model_fingerprint": "new", "pending_label": "sideways", "pending_count": 1}
    label, f = apply_hysteresis("bear", feature_ts=TUE, fingerprint="new",
                                previous_label="bull", previous_payload=payload)
    assert label == "bull" and f["pending_label"] == "bear" and f["pending_count"] == 1


def test_returning_to_the_published_label_clears_the_pending_change():
    payload = {"feature_ts": MON, "model_fingerprint": "new", "pending_label": "sideways", "pending_count": 1}
    label, f = apply_hysteresis("bull", feature_ts=WED, fingerprint="new",
                                previous_label="bull", previous_payload=payload)
    assert label == "bull" and f["pending_label"] is None and f["label_source"] == "model"


def test_an_ordinary_change_on_new_data_without_a_refit_is_published_at_once():
    label, f = apply_hysteresis("bear", feature_ts=MON, fingerprint="old", **_prev())
    assert label == "bear" and f["label_source"] == "model"


def test_first_snapshot_publishes_the_model_label():
    label, _ = apply_hysteresis("bull", feature_ts=MON, fingerprint="x", previous_label=None, previous_payload=None)
    assert label == "bull"
