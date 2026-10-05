"""Candidate-cohort track-record gate: does Discover have a proven edge?

A shortlisted candidate is BUY only when earlier Discover picks of the same
instrument type beat the passive core (EUNL.DE) by more than chance
(:func:`discover_verdict`); an "unproven" cohort is also withheld
(``Recommendation.approval_state``).

How the evidence is measured (ADR 0017, follow-up 4)
----------------------------------------------------

The gate used to run a 5-lag Newey-West t over every resolved pick, in no
particular order, keyed on the signal-weights config. On prod data that
treated as independent what is not: a run shortlists ~10 names, runs come
every ~2 trading days, and each pick's 21-day excess return shares most of
its window with the picks of neighbouring runs (443 picks from 2026-06..08
fell on only 17 dates, whose mean excess moved together, +1% for the whole
of Aug 7-12 and -1.5% to -3% for Aug 20-25). In a simulation calibrated to
those numbers a strategy with no edge was "proven" at some evaluation within
its first year in 36-63% of runs, against the ~0.1% the t > 3 bar implies.
The gate is also re-evaluated after every run, which a fixed-sample test
does not allow.

Now:

* **One observation per block** of prediction dates as long as the
  prediction horizon (21 trading days, 30 calendar days): the mean excess
  return of the block's picks. A block counts once it is over and every pick
  in it has resolved; blocks are used in order.
* **An anytime-valid sequential t-test** on those block means: the
  semi-one-sided e-process of Wang & Ramdas (arXiv 2310.03722, Thm 4.11),
  scale-invariant, so the unknown variance needs no estimate. It may be
  checked after every run: under "no edge" the chance that it EVER reaches
  ``1 / EVIDENCE_ALPHA`` is at most ``EVIDENCE_ALPHA``.
* **alpha = 1%** (owner decision 2026-09-29). In the same simulation the
  false-BUY rate for a no-skill strategy was 1.0% over three years; a real
  edge of ~1% a month (~13%/yr) is proven with probability ~3% within a
  year, ~24% within two and ~54% within three.
* **The cohort is the instrument type**, across signal-weight versions. The
  config id is a hash of the code's default weights, so keying on it reset
  the record at every weight change (09-26, and every deploy since); no
  version would ever live long enough to be judged. The question the verdict
  answers is whether Discover's shortlists beat the benchmark.
* The pick-level hit rate must still exceed 50%, so a few large winners
  cannot carry a losing majority.

The expected-maximum deflation by the global trial count (finding F15) no
longer applies: it corrected for choosing the best of many *configs*, and
the cohort is no longer a config. Every evaluation is logged.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryPrediction

logger = logging.getLogger(__name__)

#: The e-process must reach 1 / EVIDENCE_ALPHA (owner decision 2026-09-29).
EVIDENCE_ALPHA = 0.01

#: Completed blocks needed before a cohort is judged at all.
DEFAULT_MIN_BLOCKS = 3

#: A block of prediction dates as long as the 21-trading-day horizon.
_BLOCK = timedelta(days=30)

#: Prior precision of the e-process's Gaussian mixture over the standardized
#: block excess mu/sigma. c = 2 held the no-skill false-BUY rate at 1.0%
#: over three years in the prod-calibrated simulation; c = 1 let the small
#: overlap between adjacent blocks' return windows push it to 1.8%. It also
#: bounds the evidence n blocks can carry, 2 sqrt(c^2/(n+c^2))
#: ((n+c^2)/c^2)^(n/2): about 93 after 8 blocks and 224 after 9, so no
#: record is proven in less than nine months, however strong.
_MIXTURE_C = 2.0

#: A pick still pending this long after its resolve date will not resolve
#: (no prices, delisted); it is left out rather than holding its block open.
_ABANDON_AFTER = timedelta(days=14)

#: A cohort must clear this hit-rate floor (better than a coin flip) in
#: addition to the evidence test.
_MIN_HIT_RATE = 0.5


def one_sided_t_e_value(x: Sequence[float], c: float = _MIXTURE_C) -> float:
    """E-value against "mean <= 0" that grows when the mean is positive.

    Wang & Ramdas (arXiv 2310.03722), Theorem 4.11: for i.i.d. Gaussian
    observations with unknown variance,
    ``G = 2 sqrt(c^2/(n+c^2)) [(1 - S^2/((n+c^2)V))^(-n/2)
    - (1 - min(S,0)^2/((n+c^2)V))^(-n/2)]`` with ``S = sum x`` and
    ``V = sum x^2`` is an e-process for mean 0, so
    ``P(sup_n G_n >= 1/alpha) <= alpha``. Computed in logs; 0.0 when there
    is nothing to test.
    """
    n = len(x)
    if n == 0:
        return 0.0
    total = float(sum(x))
    squares = float(sum(v * v for v in x))
    if squares <= 0.0 or total <= 0.0:
        return 0.0
    denom = (n + c * c) * squares
    log_a = -0.5 * n * math.log1p(-(total * total) / denom)
    # min(S, 0) = 0 here, so the subtracted term is exactly 1.
    log_g = math.log(2.0) + 0.5 * math.log(c * c / (n + c * c)) + log_a + math.log1p(-math.exp(-log_a))
    return math.exp(min(log_g, 700.0))


def _resolve_cohort_instrument_type(features_json: dict[str, Any] | None) -> str:
    """Read the instrument_type a resolved prediction was actually scored
    under.

    Deliberately reads ``signal_breakdown.quant_signals.instrument_type``
    (with an ``"equity"`` fallback) — the path ``store_prediction`` actually
    writes (mirrors ``dossier_writer.py``'s own read at line ~239 for the LLM
    prompt context), NOT ``signal_breakdown.instrument_type`` directly, which
    the production write path (``orchestrator.py`` step 4b) never populates
    at that top level. ``get_dynamic_shrinkage_factor`` reads it through here
    too.
    """
    if not isinstance(features_json, dict):
        return "equity"
    raw_sb = features_json.get("signal_breakdown")
    signal_breakdown = raw_sb if isinstance(raw_sb, dict) else {}
    raw_qs = signal_breakdown.get("quant_signals")
    quant_signals = raw_qs if isinstance(raw_qs, dict) else {}
    return quant_signals.get("instrument_type", "equity")


#: The label a shortlisted candidate's Recommendation carries when its cohort
#: has no proven edge. "HOLD" says "keep what you own", which is wrong for a
#: name the user does not own; "WATCH" says "worth a look, not evidence enough
#: to buy" (already in the recommendation schema's verdict set).
UNPROVEN_VERDICT = "WATCH"


def discover_verdict(track_record_gate: dict[str, Any] | None) -> str:
    """BUY only when the candidate's cohort has a proven realised edge.

    The verdict used to be ``BUY`` when the composite score reached 0.65. The
    composite is a ranking score whose level moves with every formula change,
    so the share of BUYs followed the code, not the evidence: 100% of the
    shortlist on 2026-09-07..25, 0% after ADR 0017 on 09-26. Of the 503
    Discover picks from 2026-06..08 with 21 trading days of prices since,
    the 312 BUYs beat EUNL.DE by +0.03% on average (47% hit rate) and the
    191 HOLDs by +0.67% (56%); score vs excess return had rank IC -0.07.

    A BUY therefore needs what this gate measures: earlier picks of the same
    instrument type whose excess return is proven by the anytime-valid test
    at ``EVIDENCE_ALPHA``, with a hit rate above 50%.
    """
    status = (track_record_gate or {}).get("status")
    return "BUY" if status == "proven" else UNPROVEN_VERDICT


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def evaluate_track_record(
    db: Session,
    instrument_type: str,
    *,
    min_blocks: int = DEFAULT_MIN_BLOCKS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Whether Discover's picks of ``instrument_type`` have a proven edge.

    Returns a dict with ``status`` in ``{"insufficient_data", "proven",
    "unproven"}`` and the evidence behind it (``blocks``, ``e_value``,
    ``evidence_threshold``, ``mean_block_excess``, ``hit_rate``, ``n``).
    """
    clock = now or datetime.now(UTC)
    cohort = [
        r for r in db.query(DiscoveryPrediction).all()
        if r.predicted_at is not None
        and _resolve_cohort_instrument_type(r.features_json) == instrument_type
    ]
    blocks: dict[int, list[DiscoveryPrediction]] = {}
    if cohort:
        anchor = min(_utc(r.predicted_at) for r in cohort)
        for r in cohort:
            blocks.setdefault((_utc(r.predicted_at) - anchor) // _BLOCK, []).append(r)
    else:
        anchor = clock

    observations: list[float] = []
    used: list[DiscoveryPrediction] = []
    for index in sorted(blocks):
        if anchor + (index + 1) * _BLOCK > clock:
            break  # later runs may still add picks to this block
        members = blocks[index]
        if any(
            r.outcome_status != "resolved"
            and (r.resolve_at is None or _utc(r.resolve_at) + _ABANDON_AFTER > clock)
            for r in members
        ):
            break  # blocks are used in order: stop at the first still resolving
        returns = [
            (r.excess_return if r.excess_return is not None else r.realised_return)
            for r in members
            if r.outcome_status == "resolved" and r.realised_return is not None
        ]
        if returns:
            observations.append(sum(returns) / len(returns))
            used.extend(r for r in members if r.outcome_status == "resolved" and r.realised_return is not None)

    hits = [
        1.0 if (r.score_json or {}).get("hit") else 0.0
        for r in used
        if (r.score_json or {}).get("hit") is not None
    ]
    hit_rate = (sum(hits) / len(hits)) if hits else None
    e_value = one_sided_t_e_value(observations)
    threshold = 1.0 / EVIDENCE_ALPHA
    if len(observations) < min_blocks:
        status = "insufficient_data"
    elif e_value >= threshold and (hit_rate is None or hit_rate > _MIN_HIT_RATE):
        status = "proven"
    else:
        status = "unproven"

    result = {
        "status": status,
        "cohort": "instrument_type",
        "instrument_type": instrument_type,
        "n": len(used),
        "blocks": len(observations),
        "min_blocks": min_blocks,
        "mean_block_excess": round(sum(observations) / len(observations), 6) if observations else None,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "e_value": round(e_value, 4),
        "evidence_threshold": threshold,
        "method": "block_mean_e_process",
    }
    logger.info(
        "candidate_gate: %s instrument_type=%s blocks=%d/%d picks=%d e=%.3f (need %.0f) hit_rate=%s",
        status, instrument_type, len(observations), min_blocks, len(used), e_value, threshold, hit_rate,
    )
    return result
