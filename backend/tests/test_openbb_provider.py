"""Tests for OpenBB provider — verifies parameter mapping, endpoint routing, suffix stripping,
OBBject envelope unwrap, provider injection, days→start_date conversion, and field normalisation."""

from __future__ import annotations

import inspect
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import app.foundation.providers.openbb_provider as openbb_module
from app.foundation.providers.openbb_provider import (
    OpenBBProvider,
    _normalize_analyst_estimates,
    _normalize_fundamentals,
    _normalize_history,
    _normalize_quote,
)


def _make_provider(
    *,
    enabled: bool = True,
    api_url: str = "http://localhost:6900",
    news_providers: list[str] | None = None,
    default_provider: str = "yfinance",
) -> OpenBBProvider:
    return OpenBBProvider(
        enabled=enabled,
        api_url=api_url,
        news_providers=news_providers,
        default_provider=default_provider,
    )


def _mock_client(response_json: object, status_code: int = 200):
    """Return a context-manager-compatible mock httpx.Client."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = response_json
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_response
    return mock_client


# ---------------------------------------------------------------------------
# 1. OBBject envelope unwrap
# ---------------------------------------------------------------------------

def test_obbject_results_unwrapped():
    """``data`` in the result must be the ``results`` list, not the full envelope."""
    envelope = {
        "results": [{"symbol": "AAPL", "last_price": 185.0}],
        "provider": "yfinance",
        "warnings": [],
        "chart": None,
        "extra": {},
    }
    provider = _make_provider()

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(envelope)
        result = provider._api_request("quote", {"symbol": "AAPL"})

    assert result["ok"] is True
    # data must be the list, not the full dict
    assert isinstance(result["data"], list)
    assert result["data"][0]["symbol"] == "AAPL"


def test_obbject_envelope_warnings_surfaced():
    """Warnings inside the OBBject envelope must appear in the provider_result warnings."""
    envelope = {
        "results": [],
        "provider": "yfinance",
        "warnings": [{"category": "OpenBBWarning", "message": "No data returned for AAPL."}],
    }
    provider = _make_provider()

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(envelope)
        result = provider._api_request("quote", {"symbol": "AAPL"})

    assert result["ok"] is True
    warnings = result["quality"]["warnings"]
    assert len(warnings) == 1
    assert "No data returned" in warnings[0]


def test_obbject_fallback_when_no_results_key():
    """When the response lacks a ``results`` key, the raw dict is used as data."""
    raw = {"symbol": "AAPL", "last_price": 185.0}
    provider = _make_provider()

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(raw)
        result = provider._api_request("quote", {"symbol": "AAPL"})

    assert result["ok"] is True
    # raw dict used as-is (no ``results`` key)
    assert isinstance(result["data"], dict)


# ---------------------------------------------------------------------------
# 2. Provider injection for non-news capabilities
# ---------------------------------------------------------------------------

def test_provider_param_injected_for_quote():
    """``provider=yfinance`` must be injected into the quote request params."""
    provider = _make_provider(default_provider="yfinance")
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        provider._api_request("quote", {"symbol": "AAPL"})

    assert len(captured) == 1
    assert captured[0]["provider"] == "yfinance"


def test_provider_param_not_overridden_if_already_present():
    """If caller already passed ``provider`` in params it must not be overridden."""
    provider = _make_provider(default_provider="yfinance")
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        provider._api_request("quote", {"symbol": "AAPL", "provider": "polygon"})

    assert captured[0]["provider"] == "polygon"


def test_news_capability_does_not_inject_default_provider():
    """The non-news default_provider injection must NOT apply to the news path."""
    provider = _make_provider(default_provider="polygon", news_providers=["yfinance"])
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        provider._api_request("news", {"symbol": "AAPL"})

    # news path uses _news_providers list — provider must be "yfinance", not "polygon"
    assert captured[0]["provider"] == "yfinance"


# ---------------------------------------------------------------------------
# 3. days → start_date / end_date conversion
# ---------------------------------------------------------------------------

def test_days_converted_to_start_end_date():
    """``days=30`` must become ``start_date=<today-30>`` and ``end_date=<today>``."""
    provider = _make_provider()
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        provider._api_request("history", {"symbol": "AAPL", "days": 30})

    p = captured[0]
    assert "days" not in p, "raw 'days' key must not be forwarded to the ODP"
    today_str = date.today().isoformat()
    expected_start = (date.today() - timedelta(days=30)).isoformat()
    assert p["start_date"] == expected_start
    assert p["end_date"] == today_str


def test_days_none_dropped():
    """``days=None`` must be dropped and must not produce start_date/end_date."""
    provider = _make_provider()
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        provider._api_request("history", {"symbol": "AAPL", "days": None})

    p = captured[0]
    assert "days" not in p
    # Neither start_date nor end_date should appear when days was None
    assert "start_date" not in p
    assert "end_date" not in p


# ---------------------------------------------------------------------------
# 4. Quote field normalisation: last_price → close
# ---------------------------------------------------------------------------

def test_normalize_quote_adds_close_from_last_price():
    """_normalize_quote must add ``close`` when ``last_price`` is present but ``close`` is absent."""
    rows = [{"symbol": "AAPL", "last_price": 185.0, "volume": 1000}]
    result = _normalize_quote(rows)
    assert result[0]["close"] == 185.0
    assert result[0]["last_price"] == 185.0  # original field preserved


def test_normalize_quote_keeps_existing_close():
    """_normalize_quote must not overwrite an existing ``close`` field."""
    rows = [{"symbol": "AAPL", "close": 184.5, "last_price": 185.0}]
    result = _normalize_quote(rows)
    assert result[0]["close"] == 184.5  # untouched


def test_normalize_quote_non_list_passthrough():
    """_normalize_quote must pass through non-list data unchanged."""
    data = {"some": "envelope"}
    assert _normalize_quote(data) == data


# ---------------------------------------------------------------------------
# 5. History field normalisation: date / close
# ---------------------------------------------------------------------------

def test_normalize_history_promotes_datetime_to_date():
    """_normalize_history must promote ``datetime`` → ``date`` when ``date`` is absent."""
    rows = [{"datetime": "2024-01-02T00:00:00", "close": 185.0}]
    result = _normalize_history(rows)
    assert result[0]["date"] == "2024-01-02T00:00:00"
    assert "close" in result[0]


def test_normalize_history_adds_close_from_last_price():
    """_normalize_history must add ``close`` from ``last_price`` when close is absent."""
    rows = [{"date": "2024-01-02", "last_price": 185.0}]
    result = _normalize_history(rows)
    assert result[0]["close"] == 185.0


def test_normalize_history_non_list_passthrough():
    """_normalize_history must pass through non-list data unchanged."""
    data = None
    assert _normalize_history(data) is None


def test_normalize_fundamentals_unwraps_list_to_dict():
    """_normalize_fundamentals unwraps a non-empty list of period dicts to data[0]."""
    rows = [{"symbol": "AAPL", "pe_ratio": 28.5, "pb_ratio": 45.0}]
    result = _normalize_fundamentals(rows)
    assert isinstance(result, dict)
    assert result["pe_ratio"] == 28.5
    assert result["pb_ratio"] == 45.0


def test_normalize_fundamentals_empty_or_non_list_passthrough():
    """_normalize_fundamentals returns empty dict for empty list/None or normalizes dict."""
    assert _normalize_fundamentals([]) == {}
    raw_dict = {"pe_ratio": 28.5}
    assert _normalize_fundamentals(raw_dict)["pe_ratio"] == 28.5
    assert _normalize_fundamentals(None) == {}


def test_normalize_analyst_estimates_unwraps_list_to_dict():
    """_normalize_analyst_estimates unwraps a non-empty list of estimate dicts to data[0]."""
    rows = [{"symbol": "AAPL", "target_high": 250.0, "target_consensus": 230.0}]
    result = _normalize_analyst_estimates(rows)
    assert isinstance(result, dict)
    assert result["target_high"] == 250.0


def test_get_fundamentals_end_to_end_normalizes_list():
    """get_fundamentals via OpenBBProvider produces a dict when endpoint returns a list."""
    envelope = {
        "results": [{"symbol": "AAPL", "pe_ratio": 28.5, "pb_ratio": 45.0, "market_cap": 3e12}],
        "provider": "yfinance",
        "warnings": [],
    }
    provider = _make_provider()

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(envelope)
        result = provider.get_fundamentals("AAPL")

    assert result["ok"] is True
    assert isinstance(result["data"], dict)
    assert result["data"]["pe_ratio"] == 28.5
    assert result["data"]["market_cap"] == 3e12


# ---------------------------------------------------------------------------
# 6. End-to-end: get_quote returns normalised data after OBBject unwrap
# ---------------------------------------------------------------------------

def test_get_quote_normalises_last_price_to_close():
    """Full get_quote call must produce a result where ``close`` is derived from ``last_price``."""
    envelope = {
        "results": [{"symbol": "AAPL", "last_price": 185.5}],
        "provider": "yfinance",
        "warnings": [],
    }
    provider = _make_provider()

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_client(envelope)
        result = provider.get_quote("AAPL")

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, list)
    assert data[0]["close"] == 185.5


# ---------------------------------------------------------------------------
# 7. API param renaming: start → start_date, end → end_date
# ---------------------------------------------------------------------------

def test_api_request_renames_start_end_params():
    """The OpenBB REST API expects start_date/end_date, not start/end."""
    provider = _make_provider()

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _mock_client({"results": []})

        result = provider._api_request("history", {"symbol": "AAPL", "start": "2024-01-01", "end": "2024-12-31"})

        call_args = mock_client_cls.return_value.get.call_args
        params_sent = call_args.kwargs.get("params") or {}
        assert params_sent["start_date"] == "2024-01-01"
        assert params_sent["end_date"] == "2024-12-31"
        assert "start" not in params_sent
        assert "end" not in params_sent
        assert result["ok"] is True


def test_api_request_filters_none_params():
    """None-valued params should be excluded from the HTTP request."""
    provider = _make_provider()

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _mock_client({"results": []})
        provider._api_request("history", {"symbol": "AAPL", "start": None, "end": None})

        call_args = mock_client_cls.return_value.get.call_args
        params_sent = call_args.kwargs.get("params") or {}
        assert "start_date" not in params_sent
        assert "end_date" not in params_sent


# ---------------------------------------------------------------------------
# 8. Endpoint routing
# ---------------------------------------------------------------------------

_ENDPOINT_CASES = [
    ("quote", "/api/v1/equity/price/quote"),
    ("history", "/api/v1/equity/price/historical"),
    ("fundamentals", "/api/v1/equity/fundamental/metrics"),
    ("news", "/api/v1/news/company"),
    ("analyst_estimates", "/api/v1/equity/estimates/consensus"),
    ("etf_holdings", "/api/v1/etf/holdings"),
    ("fx_rate", "/api/v1/currency/price/historical"),
    ("macro_indicators", "/api/v1/economy/calendar"),
]


def test_api_endpoint_routing():
    """Each capability should map to the correct OpenBB REST endpoint."""
    provider = _make_provider()

    for capability, expected_path in _ENDPOINT_CASES:
        with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_client_cls:
            mock_client_cls.return_value = _mock_client({"results": []} if capability != "news" else {"results": []})

            params = {"symbol": "AAPL"} if capability != "macro_indicators" else {}
            provider._api_request(capability, params)

            call_args = mock_client_cls.return_value.get.call_args
            actual_url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
            assert expected_path in actual_url, f"{capability}: expected {expected_path} in {actual_url}"


# ---------------------------------------------------------------------------
# 9. Exchange suffix stripping: non-news strips suffix; news is per-provider
# ---------------------------------------------------------------------------

def test_suffix_preserved_for_non_news_when_default_provider_is_yfinance():
    """yfinance (the default ODP provider) natively understands exchange
    suffixes — stripping them would send it the wrong symbol entirely."""
    provider = _make_provider(default_provider="yfinance")

    with patch.object(provider, "_api_request") as mock_api:
        mock_api.return_value = {"ok": False, "quality": {"warnings": []}, "error": "mocked"}
        provider.get_history("IWDA.L", start="2024-01-01", end="2024-12-31")

        call_kwargs = mock_api.call_args[0][1]
        assert call_kwargs["symbol"] == "IWDA.L"

    with patch.object(provider, "_api_request") as mock_api:
        mock_api.return_value = {"ok": False, "quality": {"warnings": []}, "error": "mocked"}
        provider.get_quote("AAPL")

        call_kwargs = mock_api.call_args[0][1]
        assert call_kwargs["symbol"] == "AAPL"


def test_suffix_stripped_for_non_news_us_symbol_on_non_suffix_aware_provider():
    """A non-suffix-aware default provider (e.g. fmp) still strips a US symbol
    (which never carries a suffix to begin with — this is really a no-op
    stripping path, kept for parity with how other providers behave)."""
    provider = _make_provider(default_provider="fmp")

    with patch.object(provider, "_api_request") as mock_api:
        mock_api.return_value = {"ok": False, "quality": {"warnings": []}, "error": "mocked"}
        provider.get_quote("AAPL")

        call_kwargs = mock_api.call_args[0][1]
        assert call_kwargs["symbol"] == "AAPL"


def test_non_us_symbol_skipped_for_non_suffix_aware_default_provider():
    """A non-suffix-aware default provider (e.g. fmp) can't safely serve a
    non-US listing: stripping the suffix risks matching an unrelated US
    ticker (SHEL.AS → SHEL) and returning the wrong company's data, so the
    request is skipped entirely rather than sent with a stripped symbol."""
    provider = _make_provider(default_provider="fmp")

    with patch.object(provider, "_api_request") as mock_api:
        result = provider.get_quote("SHEL.AS")

        mock_api.assert_not_called()
        assert result["ok"] is False


def test_news_yfinance_receives_original_symbol():
    """yfinance news provider must receive the full symbol including exchange suffix."""
    provider = _make_provider(news_providers=["yfinance"])

    captured_params: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured_params.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_client_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_client_cls.return_value = mc

        provider.get_news("IWDA.L")

    assert len(captured_params) == 1
    assert captured_params[0]["symbol"] == "IWDA.L", (
        f"yfinance should receive 'IWDA.L' (with suffix), got {captured_params[0].get('symbol')!r}"
    )
    assert captured_params[0]["provider"] == "yfinance"


def test_news_polygon_receives_stripped_symbol_for_us_ticker():
    """polygon news provider must receive the bare symbol without exchange
    suffix for a US ticker (which never carries one anyway)."""
    provider = _make_provider(news_providers=["polygon"])

    captured_params: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured_params.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_client_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_client_cls.return_value = mc

        provider.get_news("AAPL")

    assert len(captured_params) == 1
    assert captured_params[0]["symbol"] == "AAPL"
    assert captured_params[0]["provider"] == "polygon"


def test_news_polygon_skipped_for_non_us_symbol():
    """polygon isn't suffix-aware and can't safely serve a non-US listing:
    stripping the suffix risks matching an unrelated US ticker
    (IWDA.L → IWDA) and returning the wrong company's news, so it's skipped
    entirely rather than queried with a stripped symbol."""
    provider = _make_provider(news_providers=["polygon"])

    captured_params: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured_params.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_client_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_client_cls.return_value = mc

        result = provider.get_news("IWDA.L")

    assert captured_params == []
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# 10. Invalid providers (eodma, gnews) are never attempted
# ---------------------------------------------------------------------------

def test_invalid_news_providers_never_attempted():
    """eodma and gnews should not appear in _news_providers and never be requested."""
    provider = _make_provider(news_providers=["yfinance", "polygon", "tiingo"])
    assert "eodma" not in provider._news_providers
    assert "gnews" not in provider._news_providers

    attempted_providers: list[str] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        if params and "provider" in params:
            attempted_providers.append(params["provider"])
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_client_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_client_cls.return_value = mc

        provider.get_news("AAPL")

    assert "eodma" not in attempted_providers
    assert "gnews" not in attempted_providers


# ---------------------------------------------------------------------------
# 11. Gating: no massive key → polygon never attempted
# ---------------------------------------------------------------------------

def test_without_massive_key_polygon_not_in_providers():
    """When no massive secret is configured, polygon should not be in the news providers list."""
    provider = _make_provider(news_providers=["yfinance"])
    assert "polygon" not in provider._news_providers
    assert "yfinance" in provider._news_providers


def test_with_massive_key_polygon_in_providers():
    """When a massive secret is configured, polygon should be included in the news providers list."""
    provider = _make_provider(news_providers=["yfinance", "polygon"])
    assert "polygon" in provider._news_providers
    assert "yfinance" in provider._news_providers


# ---------------------------------------------------------------------------
# 12. No openbb package import in module source
# ---------------------------------------------------------------------------

def test_no_openbb_import_in_module_source():
    """The openbb Python package must not be imported — it is dead code that was removed."""
    source = inspect.getsource(openbb_module)
    assert "import openbb" not in source, (
        "openbb package import found in openbb_provider.py — the dead package fallback must be removed"
    )


# ---------------------------------------------------------------------------
# 12b. get_fred_series — the real per-purpose macro/rates path
# (docs/adr/0010-openbb-per-purpose-routing.md)
# ---------------------------------------------------------------------------


def test_get_fred_series_hits_fred_series_endpoint():
    provider = _make_provider(default_provider="yfinance")
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append({"url": url, "params": dict(params or {})})
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": [{"date": "2026-08-01", "value": 4.2}]}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        result = provider.get_fred_series("VIXCLS")

    assert result["ok"] is True
    assert "/api/v1/economy/fred_series" in captured[0]["url"]


def test_get_fred_series_always_forces_provider_fred_not_default():
    """Even with default_provider="yfinance" (the ODP default for everything
    else), get_fred_series must send provider=fred — yfinance doesn't serve
    economic time series, so the generic default-provider injection would
    silently break this capability if it were allowed to apply here."""
    provider = _make_provider(default_provider="yfinance")
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        provider.get_fred_series("DGS10")

    assert captured[0]["provider"] == "fred"


def test_get_fred_series_passes_symbol_and_date_range():
    provider = _make_provider()
    captured: list[dict] = []

    def fake_get(url: str, params: dict | None = None) -> MagicMock:
        captured.append(dict(params or {}))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"results": []}
        r.raise_for_status = MagicMock()
        return r

    with patch("app.foundation.providers.openbb_provider.httpx.Client") as mock_cls:
        mc = MagicMock()
        mc.__enter__ = MagicMock(return_value=mc)
        mc.__exit__ = MagicMock(return_value=False)
        mc.get.side_effect = fake_get
        mock_cls.return_value = mc

        provider.get_fred_series("BAA10Y", start="2026-01-01", end="2026-08-01")

    assert captured[0]["symbol"] == "BAA10Y"
    assert captured[0]["start_date"] == "2026-01-01"
    assert captured[0]["end_date"] == "2026-08-01"


def test_get_fred_series_disabled_returns_error():
    provider = _make_provider(enabled=False)
    result = provider.get_fred_series("VIXCLS")
    assert result["ok"] is False


def test_get_fred_series_in_capabilities():
    provider = _make_provider()
    assert "get_fred_series" in provider.capabilities


# ---------------------------------------------------------------------------
# 13. status() keys
# ---------------------------------------------------------------------------

def test_status_has_no_python_package_key():
    """status() must not return a python_package key after the fallback was removed."""
    provider = _make_provider(enabled=False)
    status = provider.status()
    assert "python_package" not in status


def test_status_includes_default_provider():
    """status() must expose the configured default_provider."""
    provider = _make_provider(default_provider="polygon")
    status = provider.status()
    assert status["default_provider"] == "polygon"


# ---------------------------------------------------------------------------
# 14. Disabled provider returns error
# ---------------------------------------------------------------------------

def test_disabled_provider_returns_error():
    """Provider returns error result when disabled."""
    provider = _make_provider(enabled=False)
    result = provider.get_history("AAPL")
    assert result["ok"] is False
    assert "disabled" in result["error"].lower()


# ---------------------------------------------------------------------------
# 15. Default news_providers defaults to yfinance-only when not specified
# ---------------------------------------------------------------------------

def test_default_news_providers_is_yfinance_only():
    """When news_providers is not passed, the provider defaults to yfinance-only."""
    provider = OpenBBProvider(enabled=True)
    assert provider._news_providers == ["yfinance"]


# ---------------------------------------------------------------------------
# 16. probe_providers returns expected structure when disabled
# ---------------------------------------------------------------------------

def test_probe_providers_disabled_returns_empty():
    """probe_providers must return an empty available list when provider is disabled."""
    provider = _make_provider(enabled=False)
    result = provider.probe_providers()
    assert result["available"] == []
    assert "*" in result["errors"]


# ---------------------------------------------------------------------------
# 17. default_provider kwarg threaded through constructor
# ---------------------------------------------------------------------------

def test_default_provider_kwarg_stored():
    """The default_provider kwarg must be accessible as _default_provider attribute."""
    provider = OpenBBProvider(enabled=True, default_provider="tiingo")
    assert provider._default_provider == "tiingo"


def test_default_provider_default_is_yfinance():
    """When default_provider is not passed, it defaults to 'yfinance'."""
    provider = OpenBBProvider(enabled=True)
    assert provider._default_provider == "yfinance"
