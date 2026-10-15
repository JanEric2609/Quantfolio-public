"""ETF suggestions from gaps in the owner's plan, not from momentum (plan 2026-10-12, D4).

Rule-based, not a forecast. An ETF is suggested only for a line of the plan:

* the equity core and factor tilt (their indices come from the curated core
  list and ``monthly_plan_funds.CANDIDATES``), handled by the monthly plan;
* the asset-class lines the owner sets a target for (``plan_bond_target_pct``
  with ``plan_bond_duration_bucket``, ``plan_cash_target_pct``,
  ``plan_gold_target_pct``), handled here.

An index the owner already holds, matched by ISIN or by index, is never
suggested again ("already covered by X"). Within an index one fund is chosen
by the ADR-0019 rule (``monthly_plan_funds.score_fund``). An equity index
outside the plan (FTSE 100, sectors, themes) is information only. Leveraged
and inverse products, and synthetic swap funds other than the money-market
fund, are dropped.

Suggest-only: nothing here places an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.decision.monthly_plan_funds import (
    CANDIDATES,
    FundCandidate,
    candidate_from_universe,
    score_fund,
)
from app.decision.plan_rules import bond_cost_note
from app.foundation.etf_classes import (
    BOND_CLASSES,
    EQUITY_CLASSES,
    classify_etf,
    curated_index_key,
    index_key,
    is_curated,
)

RULE_NOTE = "Rule-based, not a forecast: it fills a gap in your own plan."

#: Plan line -> (asset classes it holds, curated sleeve per duration bucket or None).
_BOND_SLEEVE_BY_BUCKET = {"short": "bond_short", "mid": "bond_mid", "long": "bond_long"}
LINE_LABELS = {"bond": "Bonds (EUR)", "cash": "Cash (money market)", "gold": "Gold"}

#: Tax notes per line; every figure-free note still carries the flags (D4).
TAX_NOTES: dict[str, str] = {
    "bond": ("Bond fund: Teilfreistellung 0 %; an accumulating fund pays the yearly Vorabpauschale "
             "(§ 18 InvStG)."),
    "cash": ("Money-market fund: Teilfreistellung 0 %; an accumulating fund pays the yearly "
             "Vorabpauschale (§ 18 InvStG)."),
    "gold": ("An ETC is not an investment fund: no Vorabpauschale and no Teilfreistellung. A sale is a "
             "private sale (§ 23 EStG): gains are taxable within 12 months of buying and tax-free after "
             "(BFH 12.05.2015, VIII R 35/14 and VIII R 4/15; BFH 16.06.2020, VIII R 7/17)."),
}
_SYNTHETIC_OK = {"money_market"}
_FUND_WORDS = ("etf", "etc", "ucits", "fund", "xetra-gold", "fonds")


@dataclass
class PlanScope:
    """Which indices and classes are lines of the owner's plan."""

    core_keys: set[str] = field(default_factory=set)
    tilt_keys: set[str] = field(default_factory=set)
    #: line -> target in percent of the book (only lines above 0)
    targets: dict[str, float] = field(default_factory=dict)
    bond_bucket: str = "short"

    def line_for(self, etf_class: str, key: str) -> str | None:
        """The plan line an ETF of this class/index belongs to, or None (information only)."""
        if etf_class in EQUITY_CLASSES:
            if key in self.core_keys:
                return "core"
            if key in self.tilt_keys:
                return "tilt"
            return None
        if etf_class in BOND_CLASSES:
            return "bond" if "bond" in self.targets else None
        if etf_class == "money_market":
            return "cash" if "cash" in self.targets else None
        if etf_class == "gold":
            return "gold" if "gold" in self.targets else None
        return None


def _pct(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def plan_scope(settings: dict[str, Any], core_isins: Iterable[str] = ()) -> PlanScope:
    """The plan's lines from public settings plus the curated core/tilt lists."""
    core_keys = {index_key(c.name, isin=c.isin) for c in CANDIDATES if c.sleeve == "core"}
    for isin in core_isins:
        key = curated_index_key(isin)
        if key:
            core_keys.add(key)
    # The emerging-markets fund completes a world core (MSCI World + EM = ACWI).
    core_keys |= {"MSCI ACWI", "MSCI ACWI IMI", "MSCI EM IMI", "MSCI EM", "FTSE Emerging"}
    tilt_keys = {index_key(c.name, isin=c.isin) for c in CANDIDATES if c.sleeve == "tilt"}
    targets = {
        line: pct for line, pct in (
            ("bond", _pct(settings.get("plan_bond_target_pct"))),
            ("cash", _pct(settings.get("plan_cash_target_pct"))),
            ("gold", _pct(settings.get("plan_gold_target_pct"))),
        ) if pct > 0
    }
    bucket = str(settings.get("plan_bond_duration_bucket") or "short").lower()
    return PlanScope(core_keys, tilt_keys, targets, bucket if bucket in _BOND_SLEEVE_BY_BUCKET else "short")


def is_fund(name: str | None, isin: str | None, universe_isins: set[str] | frozenset[str] = frozenset()) -> bool:
    """Whether a held position is a fund/ETC (so its class and index mean anything)."""
    i = (isin or "").upper()
    if i and (i in universe_isins or is_curated(i) or any(c.isin == i for c in CANDIDATES)):
        return True
    n = (name or "").lower()
    return any(w in n for w in _FUND_WORDS)


def held_index_keys(
    positions: Iterable[dict[str, Any]], universe_by_isin: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    """``index key -> label of the held fund`` for every held fund (ISIN-matched names first)."""
    universe_by_isin = universe_by_isin or {}
    out: dict[str, str] = {}
    for p in positions:
        isin = str(p.get("isin") or "").upper()
        record = universe_by_isin.get(isin)
        name = (record or {}).get("name") or p.get("name") or ""
        if not is_fund(name, isin, set(universe_by_isin)):
            continue
        key = index_key(name, isin=isin or None)
        out.setdefault(key, str(p.get("ticker") or p.get("name") or isin))
    return out


def best_per_index(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One justETF record per index key, the ADR-0019 best; leveraged/inverse and stray swaps dropped."""
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for r in records:
        name = str(r.get("name") or "")
        isin = str(r.get("isin") or "").upper() or None
        cls = classify_etf(name, isin=isin, strategy=r.get("strategy"))
        if cls == "leveraged_inverse":
            continue
        key = index_key(name, isin=isin)
        cand = candidate_from_universe(r, index=key)
        if cand.replication == "synthetic" and cls not in _SYNTHETIC_OK:
            continue
        score = score_fund(cand)
        held = best.get(key)
        if held is None or (score, -cand.ter_pct) > (held[0], -float(held[1].get("ter") or 0.0)):
            best[key] = (score, {**r, "asset_class": cls, "index_key": key, "fund_score": round(score, 4)})
    return {k: v for k, (_, v) in best.items()}


def _line_classes(line: str) -> set[str]:
    if line == "bond":
        return set(BOND_CLASSES)
    return {"money_market"} if line == "cash" else {"gold"}


def _line_fund(line: str, bucket: str, covered: dict[str, str]) -> tuple[FundCandidate | None, str | None]:
    """The curated fund for a line; the held fund when one already covers its index."""
    sleeve = _BOND_SLEEVE_BY_BUCKET[bucket] if line == "bond" else line
    ranked = sorted((c for c in CANDIDATES if c.sleeve == sleeve), key=lambda c: (-score_fund(c), c.isin))
    if not ranked:
        return None, None
    best = ranked[0]
    held_by = covered.get(index_key(best.name, isin=best.isin))
    return best, held_by


def asset_class_suggestions(
    settings: dict[str, Any],
    positions: list[dict[str, Any]],
    *,
    book_eur: float,
    universe: list[dict[str, Any]] | None = None,
    core_isins: Iterable[str] = (),
) -> dict[str, Any]:
    """Plan-gap lines for bonds, cash and gold, plus the information-only list.

    ``positions`` carry ``isin``, ``ticker``, ``name`` and ``value_eur``.
    Returns ``{"lines", "info", "covered", "rule_note", "estimate",
    "not_tax_advice"}``.
    """
    scope = plan_scope(settings, core_isins)
    band = _pct(settings.get("plan_drift_band_pp", 5))
    universe = universe or []
    by_isin = {str(r.get("isin") or "").upper(): r for r in universe if r.get("isin")}
    covered = held_index_keys(positions, by_isin)

    held_by_line: dict[str, float] = {}
    for p in positions:
        isin = str(p.get("isin") or "").upper()
        name = (by_isin.get(isin) or {}).get("name") or p.get("name") or ""
        if not is_fund(name, isin, set(by_isin)):
            continue
        cls = classify_etf(name, isin=isin or None)
        for line in ("bond", "cash", "gold"):
            if cls in _line_classes(line):
                held_by_line[line] = held_by_line.get(line, 0.0) + float(p.get("value_eur") or 0.0)

    lines: list[dict[str, Any]] = []
    for line, target in scope.targets.items():
        current = 100.0 * held_by_line.get(line, 0.0) / book_eur if book_eur > 0 else 0.0
        gap = target - current
        under = gap > 0 and (current == 0.0 or gap > band)
        fund, held_by = _line_fund(line, scope.bond_bucket, covered)
        entry: dict[str, Any] = {
            "line": line,
            "label": LINE_LABELS[line],
            "target_pct": round(target, 2),
            "current_pct": round(current, 2),
            "gap_pp": round(gap, 2),
            "status": "under" if under else "ok",
            "tax_note": TAX_NOTES[line],
            "rule_note": RULE_NOTE,
        }
        if line == "bond":
            entry["duration_bucket"] = scope.bond_bucket
            entry["evidence"] = bond_cost_note(target)
        if under and fund is not None:
            entry["fund"] = {"isin": fund.isin, "name": fund.name, "why": _fund_why(fund, held_by)}
            entry["amount_eur"] = round(gap / 100.0 * book_eur, 2)
        if line == "gold" and target > 5.0:
            entry["warning"] = "Gold above 5 % of the book is more than a diversifier."
        lines.append(entry)

    info = []
    for key, r in best_per_index(universe).items():
        cls = r["asset_class"]
        if cls not in EQUITY_CLASSES or scope.line_for(cls, key) is not None or key in covered:
            continue
        info.append({"isin": r.get("isin"), "name": r.get("name"), "index": key, "asset_class": cls,
                     "reason": "Outside your plan: information only, never shortlisted."})
    info.sort(key=lambda r: str(r["index"]))

    return {
        "lines": lines,
        "info": info[:40],
        "covered": [{"index": k, "held_by": v} for k, v in sorted(covered.items())],
        "rule_note": RULE_NOTE,
        "estimate": True,
        "not_tax_advice": True,
    }


def _fund_why(fund: FundCandidate, held_by: str | None) -> str:
    from app.decision.monthly_plan_funds import _why

    why = _why(fund, score_fund(fund))
    if held_by:
        why = f"Already covered by {held_by}: add to it rather than open a second fund. " + why
    return why
