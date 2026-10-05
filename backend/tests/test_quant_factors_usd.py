"""restate_in_usd: local-currency returns in USD for Ken French's USD factors."""
import pytest

from app.foundation.quant_factors import restate_in_usd


def test_a_flat_rate_is_the_identity():
    out, dates = restate_in_usd([0.01, -0.02], ["2026-01-02", "2026-01-05"], {"2026-01-01": 1.1, "2026-01-02": 1.1, "2026-01-05": 1.1}, first_start="2026-01-01")
    assert out == pytest.approx([0.01, -0.02])
    assert dates == ["2026-01-02", "2026-01-05"]


def test_a_rising_euro_adds_to_the_usd_return():
    # EUR +1% on the day and EURUSD 1.10 -> 1.111 (+1%): about +2.01% in USD.
    out, _ = restate_in_usd([0.01], ["2026-01-02"], {"2026-01-01": 1.10, "2026-01-02": 1.111}, first_start="2026-01-01")
    assert out[0] == pytest.approx(1.01 * 1.01 - 1)


def test_missing_rates_carry_forward_and_no_start_drops_the_first_row():
    rates = {"2026-01-01": 1.0, "2026-01-05": 1.02}
    out, dates = restate_in_usd([0.0, 0.0, 0.0], ["2026-01-02", "2026-01-05", "2026-01-06"], rates)
    # First row dropped (no start rate); the 01-05 move applies once; 01-06 carries 1.02.
    assert dates == ["2026-01-05", "2026-01-06"]
    assert out == pytest.approx([0.02, 0.0])


def test_lengths_must_match():
    with pytest.raises(ValueError):
        restate_in_usd([0.01], [], {})
