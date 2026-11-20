"""`/api/portfolio/performance` must not fabricate zeroed return series (issue #134).

The previous `performance_stub` returned hardcoded `day_change`/`twr`/period
values of 0.0, which are indistinguishable from a real flat-return portfolio and
misled users. Until a real TWR series is wired for this holdings-based portfolio,
the endpoint reports the genuine invested value and an explicit
`not_implemented` marker instead of fake numbers.
"""
from decimal import Decimal

from app.foundation.portfolio_service import portfolio_performance


def _holding(qty: str, price: str | None):
    from app.foundation.models.entities import Holding

    return Holding(
        portfolio_id="p",
        quantity=Decimal(qty),
        avg_buy_price=Decimal(price) if price is not None else None,
    )


def test_performance_reports_real_invested_value():
    holdings = [_holding("10", "100"), _holding("5", "20")]
    result = portfolio_performance(holdings)
    assert result["total_value"] == 1100.0
    assert result["currency"] == "EUR"


def test_performance_flags_not_implemented_instead_of_fake_zeros():
    result = portfolio_performance([_holding("1", "50")])
    # Explicit marker so the client can distinguish "not computed" from "0% return".
    assert result["not_implemented"] is True
    # No fabricated return series masquerading as real data.
    assert "twr" not in result
    assert "day_change" not in result
    assert "periods" not in result


def test_performance_empty_portfolio():
    result = portfolio_performance([])
    assert result["total_value"] == 0.0
    assert result["not_implemented"] is True


def test_performance_excludes_unknown_cost_basis_holdings():
    holdings = [_holding("10", "100"), _holding("5", None)]
    result = portfolio_performance(holdings)
    # Only the known-cost holding contributes to invested value.
    assert result["total_value"] == 1000.0
    assert result["unknown_cost_basis_holdings"] == 1
