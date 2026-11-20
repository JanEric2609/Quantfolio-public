"""Composite time-weighted return that reweights across membership changes (P5).

A composite groups several portfolios. When a portfolio joins or leaves the
composite mid-period, that is a *reweighting* of the composite — not an external
cashflow into it. The plain ``time_weighted_return`` over the summed member
values would book a joining portfolio's whole market value as an instant gain
(and a leaving portfolio's value as a loss), distorting the composite return.

This module computes the composite return correctly: split the analysis window
at every membership-change boundary, compute each sub-period's return on the
member set valid during that sub-period (from the summed member-value series,
cashflow-adjusted), and geometrically link the sub-period returns. Within a
sub-period the membership is constant, so it reduces exactly to the ordinary
``time_weighted_return`` — the reweighting lives only at the boundaries.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from app.lab.performance_ledger.twr import time_weighted_return


def _value_as_of(snapshots: Sequence[Mapping[str, Any]], target: date) -> float | None:
    """Latest snapshot value on or before ``target`` (snapshots need not be sorted)."""
    best_date: date | None = None
    best_value: float | None = None
    for snap in snapshots:
        d = snap.get("date")
        if not isinstance(d, date) or d > target:
            continue
        if best_date is None or d > best_date:
            best_date = d
            best_value = float(snap["value"])
    return best_value


def _effective_window(
    membership: Mapping[str, Any], period_start: date, period_end: date
) -> tuple[date, date | None]:
    """Membership window clamped to genuine *in-window* transitions.

    ``valid_from``/``valid_to`` are often administrative (e.g. the composite row
    was created "today"), not economic join/leave dates. Only a transition that
    falls strictly inside the analysis window is a real reweighting boundary; a
    ``valid_from`` at/after ``period_end`` (or at/before ``period_start``) just
    means "present for the whole window", and a ``valid_to`` at/after
    ``period_end`` means "still a member through the end".
    """
    vf = membership["valid_from"]
    vt = membership.get("valid_to")
    eff_from = vf if period_start < vf < period_end else period_start
    if vt is None or vt >= period_end:
        eff_to: date | None = None
    else:
        eff_to = vt  # strictly inside → genuine leave; at/before start → inactive
    return eff_from, eff_to


def _is_active(eff_from: date, eff_to: date | None, sub_start: date, sub_end: date) -> bool:
    """Whether an effective window covers the whole sub-period [sub_start, sub_end].

    Because sub-periods are delimited by membership-change boundaries, a
    membership either covers a sub-period entirely or not at all.
    """
    if eff_from > sub_start:
        return False
    if eff_to is not None and eff_to < sub_end:
        return False
    return True


def composite_time_weighted_return(
    member_snapshots: Mapping[str, Sequence[Mapping[str, Any]]],
    memberships: Sequence[Mapping[str, Any]],
    period_start: date,
    period_end: date,
    cashflows: Sequence[Mapping[str, Any]] | None = None,
) -> float:
    """Geometrically-linked composite TWR honouring effective-dated membership.

    Args:
        member_snapshots: ``{portfolio_id: [{"date", "value"}, ...]}``.
        memberships: ``[{"portfolio_id", "valid_from", "valid_to"}]`` — an open
            membership has ``valid_to`` None.
        period_start / period_end: analysis window (inclusive).
        cashflows: external ``[{"date", "amount"}]`` deposits(+)/withdrawals(-),
            applied within each membership sub-period (not treated as membership
            reweightings).

    Returns:
        Composite TWR as a decimal (0.10 == +10%).
    """
    if not member_snapshots or not memberships or period_end <= period_start:
        return 0.0

    flows = list(cashflows or [])

    # Clamp each membership to its genuine in-window transitions.
    windows = [
        (m["portfolio_id"], *_effective_window(m, period_start, period_end))
        for m in memberships
    ]

    # Boundary dates: window ends plus every genuine membership change inside it.
    boundaries: set[date] = {period_start, period_end}
    for _, eff_from, eff_to in windows:
        for b in (eff_from, eff_to):
            if isinstance(b, date) and period_start < b < period_end:
                boundaries.add(b)
    ordered = sorted(boundaries)

    linked = 1.0
    saw_period = False
    for sub_start, sub_end in zip(ordered, ordered[1:]):
        active_ids = [
            pid
            for pid, eff_from, eff_to in windows
            if _is_active(eff_from, eff_to, sub_start, sub_end)
        ]
        # Combined active-member value series over the sub-period. Membership is
        # constant here, so a plain (cashflow-aware) TWR over the summed series
        # is the correct sub-period return; the reweighting is the boundary itself.
        sub_dates = sorted(
            {
                snap["date"]
                for pid in active_ids
                for snap in member_snapshots.get(pid, [])
                if isinstance(snap.get("date"), date)
                and sub_start <= snap["date"] <= sub_end
            }
            | {sub_start, sub_end}
        )
        combined = []
        for d in sub_dates:
            total = 0.0
            have = False
            for pid in active_ids:
                v = _value_as_of(member_snapshots.get(pid, []), d)
                if v is not None:
                    total += v
                    have = True
            if have:
                combined.append({"date": d, "value": total})
        if len(combined) < 2 or combined[0]["value"] <= 0:
            continue
        sub_flows: list[dict] = [
            {"date": f["date"], "amount": f["amount"]}
            for f in flows
            if isinstance(f.get("date"), date) and sub_start < f["date"] <= sub_end
        ]
        sub_return = time_weighted_return(combined, sub_flows, sub_start, sub_end)
        saw_period = True
        linked *= 1.0 + sub_return

    if not saw_period:
        return 0.0
    return linked - 1.0
