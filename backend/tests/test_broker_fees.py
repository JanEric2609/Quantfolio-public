from app.foundation.broker_fees import dkb_order_fee, is_prime_etf, order_fee_eur, scalable_order_fee


def test_prime_etf_from_250_is_free_at_scalable():
    assert scalable_order_fee(250.0, "iShares Core MSCI World UCITS ETF") == 0.0
    assert scalable_order_fee(600.0, "Vanguard FTSE All-World (Acc)") == 0.0


def test_prime_etf_sale_always_pays_099():
    assert scalable_order_fee(5_000.0, "iShares Core MSCI World UCITS ETF", "sell") == 0.99
    assert order_fee_eur("scalable", 5_000.0, "Vanguard FTSE All-World", side="sell") == 0.99
    assert order_fee_eur("scalable", 5_000.0, "Vanguard FTSE All-World") == 0.0


def test_below_250_or_non_prime_pays_099():
    assert scalable_order_fee(249.99, "iShares Core MSCI World") == 0.99
    assert scalable_order_fee(1000.0, "NVIDIA") == 0.99
    assert scalable_order_fee(1000.0, None) == 0.99
    assert scalable_order_fee(0.0, "NVIDIA") == 0.0


def test_dkb_tiers_and_dkb_booking_text_issuer():
    assert dkb_order_fee(5_000) == 10.0
    assert dkb_order_fee(5_000.01) == 15.0
    assert dkb_order_fee(20_000.01) == 30.0
    assert order_fee_eur("dkb", 100.0, "iShares Core MSCI World") == 10.0
    # DKB's booking text abbreviates iShares III plc as ISHSIII.
    assert is_prime_etf("WKN A0RPWH Gesch.Art KV ISHSIII")
    assert not is_prime_etf("SAP SE")
