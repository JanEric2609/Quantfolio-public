"""Deflated Sharpe and PBO over the pre-registered trials of steps 2 and 3.

Reads the monthly records the earlier steps wrote next to the panel and
groups them into families, one per question a selection could be made in:

- ``pooled_long_short``: step 2's tercile long-short per score. Is there a
  signal at all?
- ``satellite_after_tax``: step 3's satellite over the core, after costs and
  German tax, per broker and score. This is the family that gates money.
- ``satellite_tax_free``: the same after costs only. While a
  Nichtveranlagungsbescheinigung covers the depot neither sleeve pays tax, so
  this is the record for those years. It is reported, never gating: the
  ten-year horizon is mostly taxed years.

Each candidate gets its Deflated Sharpe Ratio against the global trial count,
each family its PBO over the months every candidate covers. The satellite
unlocks only when a mined score (not the value + momentum baseline) passes
DSR in the gating family and that family's PBO is measured and passes.

With ``persist`` the verdict is also stored as an ``EvidenceGateRun`` row,
which is what the monthly plan reads (report Phase 4).
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.data_engineering.paths import get_panel_dir
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.foundation.models.entities import EvidenceGateRun
from app.foundation.models.entities._core import now_utc
from app.foundation.quant_metrics import (
    deflated_sharpe_ratio,
    newey_west_t_stat,
    probability_of_backtest_overfitting,
    resolve_n_trials,
    sharpe_ratio,
)
from app.lab import pooled_model, satellite
from app.lab.evidence_gate.spec import DSR_PASS, NW_LAGS, PBO_MAX, PBO_PARTITIONS, PERIODS_PER_YEAR
from app.lab.factor_premia.strategies import REGIONS

logger = logging.getLogger(__name__)

GATING_FAMILY = "satellite_after_tax"
QUESTIONS = {
    "pooled_long_short": "Does the score rank stocks at all (tercile long-short, gross)?",
    GATING_FAMILY: "Does the satellite beat the core after costs and German tax?",
    "satellite_tax_free": "Does it beat the core after costs while no tax is due (NV certificate)?",
}


@dataclass
class Candidate:
    name: str
    mined: bool
    months: int
    first_month: str | None
    last_month: str | None
    mean_annual: float | None
    sharpe_annual: float | None
    t: float | None
    dsr: float
    passes_dsr: bool


@dataclass
class Family:
    name: str
    question: str
    gates_money: bool
    common_months: int
    # None when there are too few months or candidates to measure; that
    # fails closed but is not a measured 100 %.
    pbo: float | None
    passes_pbo: bool
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class GateResult:
    region: str
    n_trials: int
    families: list[Family]
    satellite_unlocked: bool
    reason: str
    # The mined "<broker>:<model>" candidates that unlocked the satellite.
    unlocked_by: list[str] = field(default_factory=list)
    artifact: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["artifact"] = str(self.artifact) if self.artifact else None
        return out


def pbo_or_none(wide: pd.DataFrame) -> tuple[float | None, int]:
    """CSCV PBO over the months every column covers, None when not measurable."""
    common = wide.dropna(how="any")
    if wide.shape[1] < 2 or len(common) < 2 * PBO_PARTITIONS:
        return None, len(common)
    return float(probability_of_backtest_overfitting(common.to_numpy(), PBO_PARTITIONS)), len(common)


def _candidate(name: str, values: pd.Series, mined: bool, n_trials: int) -> Candidate:
    s = values.dropna()
    r = s.to_numpy(dtype=float)
    n = len(r)
    dsr = float(deflated_sharpe_ratio(r.tolist(), n_trials, periods_per_year=PERIODS_PER_YEAR)) if n >= 3 else 0.0
    return Candidate(
        name=name, mined=mined, months=n,
        first_month=str(s.index[0]) if n else None, last_month=str(s.index[-1]) if n else None,
        mean_annual=float(r.mean() * PERIODS_PER_YEAR) if n else None,
        sharpe_annual=float(sharpe_ratio(r.tolist(), 0.0, PERIODS_PER_YEAR)) if n >= 2 else None,
        t=float(newey_west_t_stat(r, NW_LAGS)) if n > NW_LAGS + 1 else None,
        dsr=dsr, passes_dsr=dsr >= DSR_PASS,
    )


def grade_family(name: str, wide: pd.DataFrame, mined: set[str], n_trials: int, gates_money: bool = False) -> Family:
    """Grade one family: ``wide`` is month x candidate returns."""
    pbo, common = pbo_or_none(wide)
    return Family(
        name=name, question=QUESTIONS.get(name, name), gates_money=gates_money, common_months=common,
        pbo=pbo, passes_pbo=pbo is not None and pbo <= PBO_MAX,
        candidates=[_candidate(str(c), cast(pd.Series, wide[c]), str(c) in mined, n_trials) for c in wide.columns],
    )


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def pooled_wide(results: dict[str, Any]) -> pd.DataFrame:
    frame = pd.DataFrame(results.get("series") or [])
    if frame.empty:
        return pd.DataFrame()
    return frame.pivot_table(index="month", columns="model", values="long_short", aggfunc="first").sort_index()


def satellite_wide(results: dict[str, Any], after_tax: bool) -> pd.DataFrame:
    """Monthly excess over the core per ``broker:model``, as step 3 grades it.

    After tax, the core's deferred tax is added back as a monthly drag, the
    same way ``satellite.grade`` does; tax-free, neither sleeve pays any.
    """
    frame = pd.DataFrame(results.get("series") or [])
    if frame.empty:
        return pd.DataFrame()
    frame["name"] = frame["broker"] + ":" + frame["model"]
    if after_tax:
        core_tax = {f"{c['broker']}:{c['model']}": c.get("core_tax_annual") or 0.0 for c in results.get("cards", [])}
        frame["excess"] = frame["after_tax"] - frame["core"] + frame["name"].map(lambda n: core_tax.get(n, 0.0)) / 12.0
    else:
        frame["excess"] = frame["net"] - frame["core"]
    return frame.pivot_table(index="month", columns="name", values="excess", aggfunc="first").sort_index()


def _verdict(families: list[Family]) -> tuple[bool, str, list[str]]:
    gating = next((f for f in families if f.name == GATING_FAMILY), None)
    if gating is None:
        return False, "No satellite record yet: run app.lab.satellite first.", []
    passing = [c.name for c in gating.candidates if c.mined and c.passes_dsr]
    if not passing:
        best = max((c for c in gating.candidates if c.mined), key=lambda c: c.dsr, default=None)
        detail = f" (best: {best.name}, DSR {best.dsr:.2f})" if best else ""
        return False, f"No mined score's after-tax satellite passes the Deflated Sharpe test{detail}.", []
    if gating.pbo is None:
        return False, f"{', '.join(passing)} pass DSR, but the family is too short to measure PBO.", []
    if not gating.passes_pbo:
        return False, f"{', '.join(passing)} pass DSR, but PBO is {gating.pbo:.0%} (limit {PBO_MAX:.0%}).", []
    return True, f"{', '.join(passing)} pass DSR and the family's PBO is {gating.pbo:.0%}.", passing


def _finite(value: Any) -> Any:
    """JSON-safe copy: NaN and infinities become None (Postgres JSON rejects them)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def results_path(panel_dir: Path, region: str) -> Path:
    return panel_dir / "derived" / f"evidence_gate_{region}_results.json"


def run_evidence_gate(
    db: Session,
    *,
    region: str = TILT_EVIDENCE_REGION,
    panel_dir: Path | None = None,
    persist: bool = True,
) -> GateResult | None:
    """Grade every stored record of steps 2 and 3; None when neither exists.

    Nothing is added to the trial ledger, because grading a record is not a
    new trial. With ``persist`` the result is written to
    ``derived/evidence_gate_<region>_results.json`` and the verdict is stored
    as an ``EvidenceGateRun`` for the monthly plan.
    """
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; expected one of {sorted(REGIONS)}")
    panel_dir = panel_dir or get_panel_dir()
    pooled = _read(pooled_model.results_path(panel_dir, region))
    sat = _read(satellite.results_path(panel_dir, region))
    if pooled is None and sat is None:
        logger.warning("evidence_gate: no results for region %s; run pooled_model and satellite first", region)
        return None

    n_trials = resolve_n_trials(db)
    mined_models = set(pooled_model.MODELS)
    families: list[Family] = []
    if pooled is not None:
        wide = pooled_wide(pooled)
        if not wide.empty:
            families.append(grade_family("pooled_long_short", wide, mined_models, n_trials))
    if sat is not None:
        for name, after_tax in ((GATING_FAMILY, True), ("satellite_tax_free", False)):
            wide = satellite_wide(sat, after_tax)
            if wide.empty:
                continue
            mined = {c for c in wide.columns if str(c).split(":", 1)[1] in mined_models}
            families.append(grade_family(name, wide, mined, n_trials, gates_money=name == GATING_FAMILY))

    unlocked, reason, unlocked_by = _verdict(families)
    result = GateResult(region, n_trials, families, unlocked, reason, unlocked_by)
    if persist:
        computed_at = now_utc()
        record = _finite(result.as_dict())
        out = results_path(panel_dir, region)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"computed_at": computed_at.isoformat(), **record}, indent=1))
        result.artifact = out
        db.add(EvidenceGateRun(
            region=region, satellite_unlocked=unlocked, unlocked_by=unlocked_by, reason=reason,
            n_trials=n_trials, result_json=record, computed_at=computed_at,
        ))
        db.commit()
        logger.info("evidence_gate: wrote %s and stored the verdict", out)
    return result
