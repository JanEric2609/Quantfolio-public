"""Tests for composite TWR that reweights across membership changes (P5).

When a portfolio enters or leaves a composite mid-period, that is a
*reweighting* of the composite, not an external cashflow. Its entry value must
not be booked as a composite gain, nor its exit value as a loss. The composite
return is the geometrically-linked chain of sub-period returns, each computed on
the member set that was valid during that sub-period.
"""
from datetime import date

import pytest

from app.lab.performance_ledger import composite_time_weighted_return


def test_single_member_matches_its_own_return():
    member_snapshots = {
        "A": [
            {"date": date(2024, 1, 1), "value": 100.0},
            {"date": date(2024, 2, 1), "value": 120.0},
        ]
    }
    memberships = [{"portfolio_id": "A", "valid_from": date(2024, 1, 1), "valid_to": None}]

    twr = composite_time_weighted_return(
        member_snapshots, memberships, date(2024, 1, 1), date(2024, 2, 1)
    )

    assert twr == pytest.approx(0.20)


def test_member_joining_midperiod_is_not_booked_as_gain():
    # A grows 10%/month throughout; B joins on Feb 1 with a large value and also
    # grows 10%. B's €1000 entry must NOT appear as a composite gain.
    member_snapshots = {
        "A": [
            {"date": date(2024, 1, 1), "value": 100.0},
            {"date": date(2024, 2, 1), "value": 110.0},
            {"date": date(2024, 3, 1), "value": 121.0},
        ],
        "B": [
            {"date": date(2024, 2, 1), "value": 1000.0},
            {"date": date(2024, 3, 1), "value": 1100.0},
        ],
    }
    memberships = [
        {"portfolio_id": "A", "valid_from": date(2024, 1, 1), "valid_to": None},
        {"portfolio_id": "B", "valid_from": date(2024, 2, 1), "valid_to": None},
    ]

    twr = composite_time_weighted_return(
        member_snapshots, memberships, date(2024, 1, 1), date(2024, 3, 1)
    )

    # [Jan,Feb] A only: +10%. [Feb,Mar] A+B: (121+1100)/(110+1000)-1 = +10%.
    # Linked: 1.1 * 1.1 - 1 = 0.21 — NOT inflated by B's €1000 entry.
    assert twr == pytest.approx(0.21)


def test_member_leaving_midperiod_is_not_booked_as_loss():
    member_snapshots = {
        "A": [
            {"date": date(2024, 1, 1), "value": 100.0},
            {"date": date(2024, 2, 1), "value": 110.0},
            {"date": date(2024, 3, 1), "value": 121.0},
        ],
        "B": [
            {"date": date(2024, 1, 1), "value": 200.0},
            {"date": date(2024, 2, 1), "value": 220.0},
        ],
    }
    memberships = [
        {"portfolio_id": "A", "valid_from": date(2024, 1, 1), "valid_to": None},
        {"portfolio_id": "B", "valid_from": date(2024, 1, 1), "valid_to": date(2024, 2, 1)},
    ]

    twr = composite_time_weighted_return(
        member_snapshots, memberships, date(2024, 1, 1), date(2024, 3, 1)
    )

    # [Jan,Feb] A+B: 330/300-1 = +10%. [Feb,Mar] A only: 121/110-1 = +10%.
    # Linked: 1.1 * 1.1 - 1 = 0.21 — B's €220 exit is not a loss.
    assert twr == pytest.approx(0.21)


def test_no_members_returns_zero():
    assert composite_time_weighted_return({}, [], date(2024, 1, 1), date(2024, 2, 1)) == 0.0
