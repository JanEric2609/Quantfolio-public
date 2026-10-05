"""DSR and PBO over the pre-registered trials (report Phase 3, step 4).

Grades the records ``pooled_model`` and ``satellite`` wrote and says whether
any mined score may drive the satellite. See ``CONTEXT.md``.
"""

from app.lab.evidence_gate.gate import (
    GATING_FAMILY,
    Candidate,
    Family,
    GateResult,
    grade_family,
    pbo_or_none,
    results_path,
    run_evidence_gate,
)

__all__ = [
    "GATING_FAMILY",
    "Candidate",
    "Family",
    "GateResult",
    "grade_family",
    "pbo_or_none",
    "results_path",
    "run_evidence_gate",
]
