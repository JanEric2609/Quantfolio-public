"""Tests for MSCI World factor gap analysis service."""
from app.foundation.gap_analysis import compute_factor_gaps


def test_no_em_detected_as_gap_when_only_developed_markets():
    holdings = [{"ticker": "IWDA.AS", "weight": 1.0}]
    gaps = compute_factor_gaps(holdings)
    gap_names = [g["factor"] for g in gaps]
    assert "emerging_markets" in gap_names


def test_no_em_underweight_when_em_at_target():
    # EIMI.L at 12% matches the 12% benchmark target — no underweight gap
    holdings = [
        {"ticker": "IWDA.AS", "weight": 0.78},
        {"ticker": "EIMI.L", "weight": 0.12},
        {"ticker": "AGGH.L", "weight": 0.10},
    ]
    gaps = compute_factor_gaps(holdings)
    underweight_em = [g for g in gaps if g["factor"] == "emerging_markets" and g["direction"] == "underweight"]
    assert underweight_em == []


def test_overweight_factor_reported_as_overweight():
    # 100% bonds is far above the 0% bonds benchmark target
    holdings = [{"ticker": "AGGH.L", "weight": 1.0}]
    gaps = compute_factor_gaps(holdings)
    bond_gap = next((g for g in gaps if g["factor"] == "bonds"), None)
    assert bond_gap is not None
    assert bond_gap["direction"] == "overweight"


def test_gap_dict_has_required_fields():
    holdings = [{"ticker": "IWDA.AS", "weight": 1.0}]
    gaps = compute_factor_gaps(holdings)
    for g in gaps:
        assert "factor" in g
        assert "current" in g
        assert "target" in g
        assert "gap" in g
        assert "direction" in g


def test_small_gaps_below_threshold_not_reported():
    # A perfectly mixed portfolio should produce few or no gaps
    holdings = [
        {"ticker": "IWDA.AS", "weight": 0.70},
        {"ticker": "EIMI.L", "weight": 0.12},
        {"ticker": "IUSN.DE", "weight": 0.10},
        {"ticker": "AGGH.L", "weight": 0.08},
    ]
    gaps = compute_factor_gaps(holdings)
    # EM gap should be gone, small_cap gap should be gone
    gap_names = [g["factor"] for g in gaps]
    assert "emerging_markets" not in gap_names
    assert "small_cap" not in gap_names


def test_canonical_xetra_listing_resolves_like_amsterdam_listing():
    """EUNL.DE (canonical XETRA) and IWDA.AS (Amsterdam) are the same fund —
    both must resolve to the DM profile and yield identical gaps."""
    from app.foundation.gap_analysis import compute_factor_gaps as _cfg

    amsterdam = _cfg([{"ticker": "IWDA.AS", "weight": 1.0}])
    xetra = _cfg([{"ticker": "EUNL.DE", "weight": 1.0}])
    assert xetra == amsterdam
    assert "emerging_markets" in [g["factor"] for g in xetra]
