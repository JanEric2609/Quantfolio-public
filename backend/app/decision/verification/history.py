"""The trust page's historical panel: what past data says, kept apart from the live test.

ADR 0018 §1: the page judges two things and one never vouches for the other.
This module assembles the **historical** side from records other contexts have
already stored, and adds no trial of its own:

* **Factor tilt**: the pre-registered factor-premia cards (``lab.factor_premia``,
  ``factor_evidence_cards``), with each card's full-period, last-ten-years and
  haircut long-only excess side by side, and a ten-year range of outcomes
  drawn from its own yearly record re-centred on the haircut expectation.
* **Stock-ranking model**: step 2's out-of-sample model cards and the
  ``pooled_long_short`` family of the latest evidence-gate run. Strong
  evidence of ranking, but it is not live and gates no money.
* **Stock-picking satellite**: the ``satellite_after_tax`` family of the same
  run, the only one that gates money (failed while the satellite is locked).
* **Not covered**: nothing here tests Discover, the advisor, the mandates or
  anything an LLM wrote.

The factor-tilt review rule (ADR 0018 §9) applies only after 12 months of
live tilt data; until then its status says so.
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.foundation.factor_evidence import TILT_EVIDENCE_REGION, latest_evidence_gate_record
from app.foundation.quant_metrics import resolve_n_trials
from app.foundation.settings import get_public_settings
from app.lab.factor_premia import latest_cards
from app.lab.factor_premia.strategies import AFTER_TAX_FACTOR, IMPLEMENTATION_COST, PUBLICATION_DECAY

#: Data older than this many months is shown as stale.
STALE_MONTHS = 12
FAN_YEARS = 10
FAN_DRAWS = 4000
FAN_PERCENTILES = (5, 25, 50, 75, 95)
RECENT_YEARS = 10
#: Live months before the tilt review rule applies (ADR 0018 §9).
REVIEW_RULE_MONTHS = 12
_SEED = 20261007

DISCLOSURES = (
    "Simulated. Not actual results.",
    "Simulated past performance is not a reliable indicator of future performance.",
    "Simulated results have hindsight and no money at risk. They are not a forecast.",
)

NOT_COVERED = (
    "Nothing on this panel tests the Discover shortlist, the advisor or the mandates, or anything an AI model "
    "wrote. Discover ranks by a different score with no history behind it, and language models cannot be "
    "backtested honestly because they may have seen the test period. Only the live tests judge those."
)


def _month_end(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        try:
            return datetime.strptime(str(value)[:7], "%Y-%m").date()
        except ValueError:
            return None


def _stale(end: date | None, today: date) -> bool:
    if end is None:
        return True
    return (today.year - end.year) * 12 + (today.month - end.month) > STALE_MONTHS


def _fan(yearly: dict[str, float], expected: float | None) -> dict[str, Any] | None:
    """Ten-year cumulative excess drawn from the card's own years, re-centred on *expected*.

    Each draw resamples ten whole years with replacement from the yearly
    long-only excess, after shifting the record so its mean equals the
    haircut, after-cost, after-tax expectation. Seeded.
    """
    values = np.asarray([v for v in yearly.values() if isinstance(v, (int, float)) and math.isfinite(v)], dtype=float)
    if expected is None or values.size < FAN_YEARS:
        return None
    centred = values - values.mean() + float(expected)
    rng = np.random.default_rng(_SEED)
    draws = centred[rng.integers(0, values.size, size=(FAN_DRAWS, FAN_YEARS))]
    cumulative = np.prod(1.0 + draws, axis=1) - 1.0
    return {
        "years": FAN_YEARS,
        "percentiles": [
            {"p": p, "cumulative_excess": float(np.percentile(cumulative, p))} for p in FAN_PERCENTILES
        ],
        "share_beat": float(np.mean(cumulative > 0)),
    }


def _tilt_card(card: dict[str, Any], today: date) -> dict[str, Any]:
    yearly = card.get("yearly_long_only") or {}
    recent = sorted(yearly.items())[-RECENT_YEARS:]
    recent_mean = float(np.mean([v for _, v in recent])) if len(recent) >= RECENT_YEARS else None
    end = _month_end(card.get("end"))
    return {
        "strategy": card.get("strategy"),
        "label": card.get("label"),
        "region": card.get("region"),
        "gates_tilt": card.get("region") == TILT_EVIDENCE_REGION,
        "citation": card.get("citation"),
        "passed": bool(card.get("passed")),
        "long_short_t": card.get("long_short_t"),
        "t_bar": 2.0,
        "months": card.get("months"),
        "start": card.get("start"),
        "end": card.get("end"),
        "stale": _stale(end, today),
        "decay": {
            "full_period": card.get("long_only_annual"),
            "last_10_years": recent_mean,
            "after_haircut": card.get("net_expected_annual"),
        },
        "tracking_error_annual": card.get("tracking_error_annual"),
        "worst_5y_excess": card.get("worst_5y_excess"),
        "checks": card.get("checks") or [],
        "fan": _fan(yearly, card.get("net_expected_annual")),
        "computed_at": card.get("computed_at"),
    }


def _family(record: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    for f in (record or {}).get("families") or []:
        if f.get("name") == name:
            return f
    return None


def _ranking_model(record: dict[str, Any] | None, today: date) -> dict[str, Any]:
    cards = [c for c in (record or {}).get("pooled_cards") or [] if c.get("model") != "baseline"]
    family = _family(record, "pooled_long_short")
    candidates = {c.get("name"): c for c in (family or {}).get("candidates") or []}
    models = []
    for c in cards:
        cand = candidates.get(c.get("model")) or {}
        recent = c.get("recent") or {}
        models.append({
            "model": c.get("model"),
            "months": c.get("months"),
            "first_month": c.get("first_month"),
            "last_month": c.get("last_month"),
            "ic_mean": c.get("ic_mean"),
            "ic_t": c.get("ic_t"),
            "long_short_annual": c.get("long_short_annual"),
            "long_only_annual": c.get("long_only_annual"),
            "recent_months": recent.get("months"),
            "recent_ic_mean": recent.get("ic_mean"),
            "recent_long_short_annual": recent.get("long_short_annual"),
            "dsr": cand.get("dsr"),
        })
    ends = [d for m in models if (d := _month_end(m["last_month"])) is not None]
    last = max(ends, default=None)
    return {
        "available": bool(models),
        "status": "not_live",
        "models": models,
        "pbo": (family or {}).get("pbo"),
        "data_to": last.isoformat() if last else None,
        "stale": _stale(last, today) if models else False,
        "computed_at": (record or {}).get("pooled_computed_at"),
    }


def _satellite(record: dict[str, Any] | None) -> dict[str, Any]:
    family = _family(record, "satellite_after_tax")
    mined = [c for c in (family or {}).get("candidates") or [] if c.get("mined")]
    best = max(mined, key=lambda c: c.get("dsr") or 0.0, default=None)
    return {
        "available": family is not None,
        "unlocked": bool((record or {}).get("satellite_unlocked")),
        "reason": (record or {}).get("reason"),
        "dsr_bar": 0.95,
        "best": {
            "name": best.get("name"),
            "sharpe_annual": best.get("sharpe_annual"),
            "mean_annual": best.get("mean_annual"),
            "dsr": best.get("dsr"),
            "months": best.get("months"),
        } if best else None,
        "n_candidates": len(mined),
        "pbo": (family or {}).get("pbo"),
        "computed_at": (record or {}).get("computed_at"),
    }


def _tilt_review(db: Session) -> dict[str, Any]:
    isins = str(get_public_settings(db).get("plan_tilt_isins") or "").strip()
    if not isins:
        return {
            "status": "not_live",
            "note": "The tilt is not live: no tilt ETF is set in the plan settings, so there is nothing to review.",
        }
    return {
        "status": "too_early",
        "note": (
            f"The review rule applies after {REVIEW_RULE_MONTHS} months of live tilt data: the tilt goes to "
            "'under review' if its drawdown or time under water against the passive core exceeds the 95th "
            "percentile of the haircut backtest's. A review, never an automatic sale."
        ),
    }


def build_history(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """The historical panel: factor tilt, ranking model, satellite and what none of them covers."""
    today = (now or datetime.now(UTC)).date()
    record = latest_evidence_gate_record(db, TILT_EVIDENCE_REGION)
    tilt = [_tilt_card(c, today) for c in latest_cards(db)]
    try:
        n_trials = int(record["n_trials"]) if record and record.get("n_trials") else resolve_n_trials(db)
    except Exception:  # noqa: BLE001 - a missing ledger only drops the number
        n_trials = None
    return {
        "title": "Historical evidence (simulated, not live)",
        "tilt_cards": tilt,
        "ranking_model": _ranking_model(record, today),
        "satellite": _satellite(record),
        "tilt_review": _tilt_review(db),
        "n_trials": n_trials,
        "haircut": {
            "publication_decay": PUBLICATION_DECAY,
            "implementation_cost": IMPLEMENTATION_COST,
            "after_tax_factor": AFTER_TAX_FACTOR,
        },
        "not_covered": NOT_COVERED,
        "disclosures": list(DISCLOSURES),
    }


__all__ = ["build_history"]
