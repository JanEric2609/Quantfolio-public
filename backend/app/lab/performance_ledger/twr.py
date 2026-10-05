"""Time-Weighted Return (TWR) calculation.

TWR measures portfolio return independent of external cashflows,
suitable for evaluating manager performance.
"""

from datetime import date
from dataclasses import dataclass


@dataclass
class TWRPeriod:
    """Single return period in TWR calculation."""
    start_date: date
    end_date: date
    period_return: float


def time_weighted_return(
    portfolio_snapshots: list[dict],
    cashflows: list[dict] | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> float:
    """
    Calculate Time-Weighted Return.

    TWR breaks the investment period into sub-periods whenever
    external cashflows occur, calculates returns for each sub-period,
    and geometrically links them.

    Args:
        portfolio_snapshots: [{date, value}] daily snapshots (sorted by date)
        cashflows: [{date, amount}] positive = deposit, negative = withdrawal
        period_start: Analysis period start (default: first snapshot)
        period_end: Analysis period end (default: last snapshot)

    Returns:
        TWR as decimal (e.g., 0.10 for 10%)
    """
    if not portfolio_snapshots:
        return 0.0

    if cashflows is None:
        cashflows = []

    # Sort snapshots and cashflows by date
    snapshots = sorted(portfolio_snapshots, key=lambda s: s["date"])
    
    # Aggregate cashflows by date
    aggregated_flows = {}
    for f in cashflows:
        d = f["date"]
        if isinstance(d, date):
            aggregated_flows[d] = aggregated_flows.get(d, 0.0) + f["amount"]
    flows = [{"date": d, "amount": amt} for d, amt in sorted(aggregated_flows.items())]

    if period_start is None:
        period_start = snapshots[0]["date"]
    if period_end is None:
        period_end = snapshots[-1]["date"]
    assert period_start is not None
    assert period_end is not None

    # Build list of return periods (split by cashflows)
    return_periods = []
    current_start = period_start

    # Get opening value
    opening_value = None
    for snap in snapshots:
        snap_date = snap.get("date")
        if isinstance(snap_date, date) and snap_date >= current_start:
            opening_value = snap["value"]
            break

    if opening_value is None:
        return 0.0

    for flow in flows:
        flow_date = flow.get("date")
        if not isinstance(flow_date, date):
            continue
        if current_start < flow_date <= period_end:
            # Find closing value just before flow
            closing_value = None
            for snap in reversed(snapshots):
                snap_date = snap.get("date")
                if isinstance(snap_date, date) and snap_date < flow_date:
                    closing_value = snap["value"]
                    break

            if closing_value and closing_value > 0:
                period_return = (closing_value - opening_value) / opening_value
                return_periods.append(
                    TWRPeriod(
                        start_date=current_start,
                        end_date=flow_date,
                        period_return=period_return,
                    )
                )

            # Next period starts after flow (skip flow if no closing value available)
            if closing_value is not None:
                opening_value = closing_value + flow["amount"]
                current_start = flow_date
            # else: no snapshot before this flow — skip it to avoid losing accumulated value

    # Final period (from last flow to period_end)
    closing_value = None
    for snap in reversed(snapshots):
        snap_date = snap.get("date")
        if isinstance(snap_date, date) and snap_date <= period_end:
            closing_value = snap["value"]
            break

    if closing_value and opening_value > 0:
        final_return = (closing_value - opening_value) / opening_value
        return_periods.append(
            TWRPeriod(
                start_date=current_start,
                end_date=period_end,
                period_return=final_return,
            )
        )

    # Geometrically link sub-period returns
    if not return_periods:
        return 0.0

    twr = 1.0
    for period in return_periods:
        twr *= 1.0 + period.period_return

    return twr - 1.0
