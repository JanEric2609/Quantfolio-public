"""Learn tab backend: read-only helpers for the lessons (WP14).

``GET /api/learn/source`` returns the source text and line of one named
function from a fixed allow-list; the name never becomes a path or an import
string the caller controls. ``GET /api/learn/sale-tax`` runs the tax module's
own functions on one example sale, so the lesson shows the real rule and not a
copy of it. Tax answers carry ``estimate`` and ``not_tax_advice``.
"""
from __future__ import annotations

import importlib
import inspect
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.foundation.auth import current_user
from app.foundation.models.entities import User
from app.foundation.tax_calc.jurisdictions.de.allowances import KEST_RATE, SOLI_RATE
from app.foundation.tax_calc.jurisdictions.de.teilfreistellung import (
    TEILFREISTELLUNG_RATES,
    apply_teilfreistellung,
    teilfreistellung_pct_for_fund_class,
)

router = APIRouter(prefix="/api/learn", tags=["learn"])

#: lesson key -> (module, function). Only these can be shown; nothing else is importable from here.
SOURCE_ALLOW_LIST: dict[str, tuple[str, str]] = {
    "annualised_return": ("app.foundation.quant_metrics", "annualised_return"),
    "annualised_volatility": ("app.foundation.quant_metrics", "annualised_volatility"),
    "beta": ("app.foundation.quant_metrics", "beta"),
    "newey_west_t_stat": ("app.foundation.quant_metrics", "newey_west_t_stat"),
    "deflated_sharpe_ratio": ("app.foundation.quant_metrics", "deflated_sharpe_ratio"),
    "diebold_mariano_nonoverlapping": ("app.foundation.quant_metrics", "diebold_mariano_nonoverlapping"),
    "walk_forward_splits": ("app.lab.alphacrafter.tuning", "walk_forward_splits"),
    "e_processes": ("app.decision.graduation.ladder", "e_processes"),
    "beta_adjusted": ("app.decision.graduation.ladder", "beta_adjusted"),
    "shrunk_covariance": ("app.foundation.allocation", "shrunk_covariance"),
    "gbm_fan_projection": ("app.foundation.quant_mc.projection", "gbm_fan_projection"),
    "apply_teilfreistellung": ("app.foundation.tax_calc.jurisdictions.de.teilfreistellung", "apply_teilfreistellung"),
}


class SourceOut(BaseModel):
    name: str
    module: str
    file: str
    line: int
    source: str


class SaleTaxOut(BaseModel):
    proceeds_eur: float
    cost_eur: float
    gain_eur: float
    fund_class: str
    teilfreistellung_pct: float
    taxable_gain_eur: float
    kest_rate: float
    soli_rate: float
    tax_eur: float
    estimate: bool = True
    not_tax_advice: bool = True
    note: str


@router.get("/source", response_model=SourceOut)
def function_source(name: str = Query(..., max_length=64), _user: User = Depends(current_user)) -> SourceOut:
    entry = SOURCE_ALLOW_LIST.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown_function")
    module_name, func_name = entry
    func = getattr(importlib.import_module(module_name), func_name)
    lines, first = inspect.getsourcelines(func)
    path = inspect.getsourcefile(func) or ""
    # Repo-relative, so the lesson never shows a server path.
    marker = "/app/"
    rel = "app/" + path.split(marker, 1)[1] if marker in path else func_name
    return SourceOut(name=name, module=module_name, file=rel, line=first, source="".join(lines))


@router.get("/sale-tax", response_model=SaleTaxOut)
def sale_tax(
    proceeds_eur: float = Query(..., ge=0, le=1e9),
    cost_eur: float = Query(..., ge=0, le=1e9),
    fund_class: str = Query("aktien", max_length=24),
    _user: User = Depends(current_user),
) -> SaleTaxOut:
    if fund_class.lower() not in TEILFREISTELLUNG_RATES:
        raise HTTPException(status_code=422, detail="unknown_fund_class")
    gain = Decimal(str(proceeds_eur)) - Decimal(str(cost_eur))
    tf = teilfreistellung_pct_for_fund_class(fund_class)
    taxable = apply_teilfreistellung(max(gain, Decimal("0")), tf)
    kest = (taxable * KEST_RATE).quantize(Decimal("0.01"))
    soli = (kest * SOLI_RATE).quantize(Decimal("0.01"))
    note = (
        "Before your saver's allowance (Sparer-Pauschbetrag) and any church tax. "
        "Your broker's statement is the source of truth."
    )
    out: dict[str, Any] = dict(
        proceeds_eur=proceeds_eur,
        cost_eur=cost_eur,
        gain_eur=float(gain),
        fund_class=fund_class.lower(),
        teilfreistellung_pct=float(tf),
        taxable_gain_eur=float(taxable),
        kest_rate=float(KEST_RATE),
        soli_rate=float(SOLI_RATE),
        tax_eur=float(kest + soli),
        note=note,
    )
    return SaleTaxOut(**out)
