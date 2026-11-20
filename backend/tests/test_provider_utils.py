"""Tests for shared provider utilities."""

from app.foundation.providers.utils import (
    is_us_listing,
    split_exchange_suffix,
    strip_exchange_suffix,
)


def test_strips_known_exchange_suffixes():
    assert strip_exchange_suffix("IWDA.L") == "IWDA"
    assert strip_exchange_suffix("SHEL.AS") == "SHEL"
    assert strip_exchange_suffix("VWCE.DE") == "VWCE"
    assert strip_exchange_suffix("EIMI.L") == "EIMI"
    assert strip_exchange_suffix("SXR8.DE") == "SXR8"
    assert strip_exchange_suffix("ABBN.SW") == "ABBN"
    assert strip_exchange_suffix("YAR.OL") == "YAR"


def test_preserves_class_shares():
    assert strip_exchange_suffix("BRK.A") == "BRK.A"
    assert strip_exchange_suffix("BRK.B") == "BRK.B"


def test_preserves_unsuffixed_tickers():
    assert strip_exchange_suffix("AAPL") == "AAPL"
    assert strip_exchange_suffix("MSFT") == "MSFT"
    assert strip_exchange_suffix("SPY") == "SPY"


def test_preserves_special_tickers():
    assert strip_exchange_suffix("^GSPC") == "^GSPC"
    assert strip_exchange_suffix("GC=F") == "GC=F"


def test_empty_string():
    assert strip_exchange_suffix("") == ""


def test_split_exchange_suffix_maps_to_mic():
    assert split_exchange_suffix("EOAN.DE") == ("EOAN", "XETR")
    assert split_exchange_suffix("SHEL.AS") == ("SHEL", "XAMS")
    assert split_exchange_suffix("IWDA.L") == ("IWDA", "XLON")
    assert split_exchange_suffix("ABBN.SW") == ("ABBN", "XSWX")
    assert split_exchange_suffix("YAR.OL") == ("YAR", "XOSL")
    assert split_exchange_suffix("AAPL") == ("AAPL", None)
    assert split_exchange_suffix("BRK.B") == ("BRK.B", None)


def test_is_us_listing():
    assert is_us_listing("AAPL") is True
    assert is_us_listing("BRK.B") is True
    assert is_us_listing("EOAN.DE") is False
    assert is_us_listing("SHEL.AS") is False
    # Tokyo/HK listings used to pass as US ones.
    assert is_us_listing("7203.T") is False
    assert is_us_listing("0700.HK") is False


def test_listing_region_picks_the_factor_market():
    from app.foundation.providers.utils import listing_region

    assert listing_region("AAPL") == "us"
    assert listing_region("SAP.DE") == "europe"
    assert listing_region("HSBA.L") == "europe"
    assert listing_region("NOVN.SW") == "europe"
    assert listing_region("7203.T") == "japan"
    assert listing_region("0700.HK") == "other"


def test_twelvedata_symbol_params_use_mic_code():
    from app.foundation.providers.twelvedata_provider import _symbol_params

    assert _symbol_params("EOAN.DE") == {"symbol": "EOAN", "mic_code": "XETR"}
    assert _symbol_params("AAPL") == {"symbol": "AAPL"}


def test_us_only_providers_skip_eu_listings():
    """Alpaca/Databento must not strip EU suffixes — a stripped symbol can
    collide with an unrelated US ticker and return the wrong price."""
    from app.foundation.providers.alpaca_provider import AlpacaProvider
    from app.foundation.providers.databento_provider import DatabentoProvider

    alpaca = AlpacaProvider("key", "secret")
    result = alpaca.get_quote("EOAN.DE")
    assert result["ok"] is False
    assert "US listings only" in str(result["quality"]["warnings"])

    databento = DatabentoProvider("key")
    result = databento.get_quote("EOAN.DE")
    assert result["ok"] is False
    assert "US listings only" in str(result["quality"]["warnings"])


# --- Rate limiter tests ---

from app.foundation.providers.rate_limiter import PROVIDER_RATE_LIMITS, RateLimiter, RateLimitConfig


def test_tiingo_limit_matches_real_hourly_quota():
    """Tiingo's free tier is 50 requests/hour, not 50/min — (50, 60) was
    60x too permissive (Track E1)."""
    max_calls, window_seconds, _desc = PROVIDER_RATE_LIMITS["tiingo"][0]
    assert (max_calls, window_seconds) == (50, 3600)


def test_alpaca_limit_does_not_exceed_real_per_minute_quota():
    """Alpaca's free-tier data API allows 200 requests/min; the previous
    (5, 1) config allowed 300/min (Track E1)."""
    max_calls, window_seconds, _desc = PROVIDER_RATE_LIMITS["alpaca"][0]
    assert max_calls / window_seconds <= 200 / 60


def test_rate_limiter_allows_up_to_max():
    config = RateLimitConfig(max_calls=3, window_seconds=60)
    limiter = RateLimiter(config)
    assert limiter.acquire() is not None
    assert limiter.acquire() is not None
    assert limiter.acquire() is not None
    assert limiter.remaining == 0
    assert limiter.is_throttled is True
    assert limiter.acquire() is None


def test_rate_limiter_resets_after_window():
    config = RateLimitConfig(max_calls=2, window_seconds=3600)
    limiter = RateLimiter(config)
    assert limiter.acquire() is not None
    assert limiter.acquire() is not None
    assert limiter.acquire() is None
    limiter.reset()
    assert limiter.acquire() is not None


def test_rate_limiter_remaining_decreases():
    config = RateLimitConfig(max_calls=5, window_seconds=60)
    limiter = RateLimiter(config)
    assert limiter.remaining == 5
    limiter.acquire()
    assert limiter.remaining == 4
    limiter.acquire()
    assert limiter.remaining == 3


def test_rate_limiter_expires_old_timestamps():
    """RateLimiter automatically trims old timestamps after window_seconds."""
    from unittest.mock import patch

    config = RateLimitConfig(max_calls=2, window_seconds=3600, description="test")
    limiter = RateLimiter(config)

    # time.monotonic is called twice per successful acquire (once in _trim, once
    # for the recorded timestamp) and once per failed acquire (just _trim).
    # Sequence of (trim, ts) pairs drives the test:
    #   acquire 1 (success at t=100):  trim=100, ts=100
    #   acquire 2 (success at t=200):  trim=200, ts=200
    #   acquire 3 (fail    at t=300):  trim=300
    #   acquire 4 (success at t=4000): trim=4000, ts=4000  (window=3600 expired,
    #                                                          old timestamps trimmed)
    with patch(
        "app.foundation.providers.rate_limiter.time.monotonic",
        side_effect=[100.0, 100.0, 200.0, 200.0, 300.0, 4000.0, 4000.0],
    ):
        assert limiter.acquire() is not None
        assert limiter.acquire() is not None
        assert limiter.acquire() is None
        assert limiter.acquire() is not None  # old timestamps trimmed
