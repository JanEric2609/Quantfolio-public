import json
import threading
from typing import Any
from unittest import mock

import httpx
import pytest
from conftest import _memory_db

from app.foundation.providers.base import MarketDataProvider, provider_result
from app.foundation.providers.registry import (
    _DEFAULT_CHAIN,
    ProviderRegistry,
    build_provider_registry,
    invalidate_registry_cache,
)
from app.foundation.providers.tiingo_provider import TiingoProvider
from app.foundation.providers.twelvedata_provider import TwelveDataProvider
from app.foundation.providers.yfinance_provider import YFinanceProvider
from app.foundation.settings import DEFAULT_PUBLIC_SETTINGS, upsert_public_settings


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Clear the module-level registry cache before each test."""
    invalidate_registry_cache()
def test_build_provider_registry_uses_default_order_when_no_setting():
    db = _memory_db()
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names == _DEFAULT_CHAIN


def test_build_provider_registry_respects_custom_provider_chain_json():
    db = _memory_db()
    custom_chain = ["yfinance", "ecb_sdw", "fred"]
    upsert_public_settings(db, {"provider_chain_json": json.dumps(custom_chain)})
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names == custom_chain


def test_build_provider_registry_ignores_unknown_provider_names():
    db = _memory_db()
    custom_chain = ["yfinance", "unknown_provider", "ecb_sdw"]
    upsert_public_settings(db, {"provider_chain_json": json.dumps(custom_chain)})
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names == ["yfinance", "ecb_sdw"]


def test_build_provider_registry_falls_back_to_default_on_invalid_json():
    db = _memory_db()
    upsert_public_settings(db, {"provider_chain_json": "not valid json"})
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names == _DEFAULT_CHAIN


def test_build_provider_registry_falls_back_to_default_on_non_list_json():
    db = _memory_db()
    upsert_public_settings(db, {"provider_chain_json": json.dumps({"openbb": True})})
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names == _DEFAULT_CHAIN


def test_build_provider_registry_empty_chain_falls_back_to_default():
    db = _memory_db()
    upsert_public_settings(db, {"provider_chain_json": ""})
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names == _DEFAULT_CHAIN


class _FakeProvider(MarketDataProvider):
    """A controllable fake provider for testing the registry."""

    def __init__(
        self,
        name: str,
        capabilities: set[str],
        enabled: bool = True,
        fail_times: int = 0,
        succeed: bool = True,
    ):
        super().__init__(enabled=enabled)
        self.name = name
        self.capabilities = capabilities
        self._fail_times = fail_times
        self._call_count = 0
        self._succeed = succeed

    def status(self) -> dict[str, Any]:
        return {"provider": self.name, "enabled": self.enabled, "available": self.enabled}

    def get_quote(self, symbol: str) -> dict[str, Any]:
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise Exception(f"fail #{self._call_count}")
        if not self._succeed:
            raise Exception("always fails")
        return provider_result(self.name, ok=True, data={"symbol": symbol})

    def get_history(self, symbol: str, start: str | None = None, end: str | None = None, days: int | None = None) -> dict[str, Any]:
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise Exception(f"fail #{self._call_count}")
        if not self._succeed:
            raise Exception("always fails")
        return provider_result(self.name, ok=True, data={"symbol": symbol, "rows": []})


def test_first_success_skips_dead_providers():
    """After a provider fails for a capability, it should be skipped on subsequent calls."""
    p1 = _FakeProvider("p1", {"get_quote"}, fail_times=1, succeed=True)
    p2 = _FakeProvider("p2", {"get_quote"}, succeed=True)
    registry = ProviderRegistry([p1, p2])

    # First call: p1 fails, then p2 succeeds
    result1 = registry.get_quote("TEST")
    assert result1["ok"] is True
    assert result1["provider"] == "p2"
    assert p1._call_count == 1  # p1 was tried once

    # Second call: p1 is marked dead for get_quote, skipped immediately
    result2 = registry.get_quote("TEST")
    assert result2["ok"] is True
    assert result2["provider"] == "p2"
    assert p1._call_count == 1  # p1 was NOT tried again


def test_first_success_skips_when_capability_not_supported():
    """Providers that don't declare a capability should be skipped."""
    p1 = _FakeProvider("p1", {"get_history"}, succeed=False)
    p2 = _FakeProvider("p2", {"get_quote"}, succeed=True)
    registry = ProviderRegistry([p1, p2])

    result = registry.get_quote("TEST")
    assert result["ok"] is True
    assert result["provider"] == "p2"
    # p1 was skipped because it doesn't have "get_quote" in capabilities


def test_first_success_recovers_after_successful_call():
    """A provider that failed is skipped (cooldown) then retried after expiry."""
    p1 = _FakeProvider("p1", {"get_quote"}, fail_times=1, succeed=True)
    p2 = _FakeProvider("p2", {"get_quote"}, succeed=True)
    registry = ProviderRegistry([p1, p2])

    # First call: p1 fails, p2 succeeds
    result1 = registry.get_quote("TEST")
    assert result1["provider"] == "p2"
    assert p1._call_count == 1

    # Second call: p1 is in cooldown, p2 succeeds
    result2 = registry.get_quote("TEST")
    assert result2["provider"] == "p2"
    assert p1._call_count == 1  # p1 was NOT retried (in cooldown)


class _BlockingProvider(MarketDataProvider):
    def __init__(self, name: str, capabilities: set[str]):
        super().__init__(enabled=True)
        self.name = name
        self.capabilities = capabilities
        self._block_event = threading.Event()
        self._call_count = 0

    def status(self) -> dict[str, Any]:
        return {"provider": self.name, "enabled": True, "available": True}

    def get_history(
        self, symbol: str, start: str | None = None, end: str | None = None, days: int | None = None,
    ) -> dict[str, Any]:
        self._call_count += 1
        self._block_event.wait()
        return provider_result(self.name, ok=False, error="unreachable")


def test_first_success_times_out_blocking_provider():
    p_block = _BlockingProvider("blocker", {"get_history"})
    p_ok = _FakeProvider("ok_provider", {"get_history"}, succeed=True)
    registry = ProviderRegistry([p_block, p_ok])

    try:
        result = registry.first_success("get_history", "TEST", provider_timeout_s=1.0)
        assert result["ok"] is True
        assert result["provider"] == "ok_provider"
        assert p_block._call_count == 1
    finally:
        p_block._block_event.set()


def test_first_success_returns_failure_when_all_providers_timeout():
    p_block1 = _BlockingProvider("blocker1", {"get_history"})
    p_block2 = _BlockingProvider("blocker2", {"get_history"})
    registry = ProviderRegistry([p_block1, p_block2])

    try:
        result = registry.first_success("get_history", "TEST", provider_timeout_s=0.5)
        assert result["ok"] is False
        assert result["provider"] == "registry"
        assert p_block1._call_count == 1
        assert p_block2._call_count == 1
    finally:
        p_block1._block_event.set()
        p_block2._block_event.set()


def test_yfinance_get_history_times_out_on_hung_request():
    provider = YFinanceProvider()
    provider.enabled = True

    block_event = threading.Event()

    def _blocking_history(**kwargs):
        block_event.wait()
        raise RuntimeError("unreachable")

    mock_ticker = mock.MagicMock()
    mock_ticker.history = _blocking_history

    try:
        with mock.patch("yfinance.Ticker", return_value=mock_ticker), \
             mock.patch("app.foundation.providers.yfinance_provider._YF_REQUEST_TIMEOUT_S", 0.5):
            result = provider.get_history("TEST", days=365)

        assert result["ok"] is False
        assert "timed out" in str(result.get("error", "")).lower()
    finally:
        block_event.set()


# ---------------------------------------------------------------------------
# Task 5 (audit-fixes-2026-08): default chain order + unsupported-listing skips
# ---------------------------------------------------------------------------

_ALL_CHAIN_PROVIDERS = {
    "finnhub", "openbb", "twelvedata", "alpaca", "databento", "tiingo",
    "alphavantage", "fred", "eod", "ecb_sdw", "massive", "yfinance", "justetf",
}


def test_default_chain_orders_yfinance_and_justetf_before_twelvedata():
    """yfinance sits right before openbb (docs/adr/0010: quote/history/
    fundamentals traffic must keep resolving via yfinance first even once
    openbb_enabled unlocks openbb's macro-indicators capability); justetf
    precedes twelvedata/tiingo."""
    assert _DEFAULT_CHAIN.index("yfinance") + 1 == _DEFAULT_CHAIN.index("openbb")
    assert _DEFAULT_CHAIN.index("yfinance") < _DEFAULT_CHAIN.index("twelvedata")
    assert _DEFAULT_CHAIN.index("justetf") < _DEFAULT_CHAIN.index("twelvedata")
    assert _DEFAULT_CHAIN.index("justetf") < _DEFAULT_CHAIN.index("tiingo")


def test_default_chain_keeps_every_provider():
    """Reorder only — no provider dropped or renamed."""
    assert set(_DEFAULT_CHAIN) == _ALL_CHAIN_PROVIDERS
    assert len(_DEFAULT_CHAIN) == len(_ALL_CHAIN_PROVIDERS)


def test_settings_default_chain_matches_registry_default():
    assert json.loads(DEFAULT_PUBLIC_SETTINGS["provider_chain_json"]) == _DEFAULT_CHAIN


def test_build_provider_registry_uses_reordered_default_chain():
    db = _memory_db()
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names.index("yfinance") < names.index("twelvedata")
    assert names.index("justetf") < names.index("twelvedata")


def test_yfinance_precedes_openbb_so_quotes_stay_on_yfinance():
    """docs/adr/0010-openbb-per-purpose-routing.md: quote/history/fundamentals
    traffic must keep resolving via yfinance first even once openbb_enabled
    unlocks openbb's macro-indicators/get_fred_series capabilities."""
    db = _memory_db()
    registry = build_provider_registry(db)
    names = [p.name for p in registry.providers]
    assert names.index("yfinance") < names.index("openbb")


def test_get_fred_series_dispatches_via_first_success():
    registry = ProviderRegistry([])
    with mock.patch.object(registry, "first_success", return_value={"ok": True}) as mocked:
        result = registry.get_fred_series("VIXCLS", start="2026-01-01", end="2026-08-01")

    mocked.assert_called_once_with(
        "get_fred_series", "VIXCLS", start="2026-01-01", end="2026-08-01",
    )
    assert result == {"ok": True}


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


def _http_client_factory(payload: Any, calls: list[dict[str, Any]]):
    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def get(self, url: str, params: Any = None, headers: Any = None) -> _FakeResponse:
            calls.append({"url": url, "params": params, "headers": headers})
            return _FakeResponse(payload)

    return _Client


def test_dot_suffixed_listings_skip_http_on_twelvedata_and_tiingo(monkeypatch):
    """.L/.DE/… listings must short-circuit with zero HTTP calls on both providers."""
    http_calls: list[dict[str, Any]] = []

    def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
        http_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("provider attempted an HTTP call")

    monkeypatch.setattr(httpx, "Client", _forbidden_client)
    td = TwelveDataProvider("dummy-key")
    tg = TiingoProvider("dummy-key")
    for sym in ("IWDA.L", "EOAN.DE", "SHEL.AS", "RY.TO", "FOO.XYZ"):
        for result in (td.get_history(sym), tg.get_history(sym)):
            assert result["ok"] is False
            warnings = result["quality"]["warnings"]
            assert any(f"unsupported listing '{sym}'" in w for w in warnings), (sym, warnings)
    assert http_calls == []


def test_us_symbols_still_flow_on_twelvedata(monkeypatch):
    calls: list[dict[str, Any]] = []
    payload = {"values": [{
        "datetime": "2026-08-21", "open": "10", "high": "11",
        "low": "9", "close": "10.5", "volume": "1000",
    }]}
    monkeypatch.setattr(httpx, "Client", _http_client_factory(payload, calls))
    result = TwelveDataProvider("dummy-key").get_history("AAPL")
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["params"]["symbol"] == "AAPL"
    assert "mic_code" not in calls[0]["params"]
    assert str(result["data"][0]["close"]) == "10.5"


def test_us_share_class_symbols_still_flow_on_twelvedata(monkeypatch):
    """BRK.B is a US share class, not an exchange suffix — must keep flowing."""
    calls: list[dict[str, Any]] = []
    payload = {"values": [{
        "datetime": "2026-08-21", "open": "10", "high": "11",
        "low": "9", "close": "10.5", "volume": "1000",
    }]}
    monkeypatch.setattr(httpx, "Client", _http_client_factory(payload, calls))
    result = TwelveDataProvider("dummy-key").get_history("BRK.B")
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["params"]["symbol"] == "BRK.B"
    assert "mic_code" not in calls[0]["params"]


def test_us_symbols_still_flow_on_tiingo(monkeypatch):
    calls: list[dict[str, Any]] = []
    payload = [{"date": "2026-08-21T00:00:00Z", "adjClose": "10.5", "close": "10.5"}]
    monkeypatch.setattr(httpx, "Client", _http_client_factory(payload, calls))
    result = TiingoProvider("dummy-key").get_history("AAPL")
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/tiingo/daily/AAPL/prices")
    assert str(result["data"][0]["close"]) == "10.5"


def test_china_listings_still_flow_on_tiingo(monkeypatch):
    """Tiingo covers US + China (.SS Shanghai / .SZ Shenzhen)."""
    calls: list[dict[str, Any]] = []
    payload = [{"date": "2026-08-21T00:00:00Z", "adjClose": "1600", "close": "1600"}]
    monkeypatch.setattr(httpx, "Client", _http_client_factory(payload, calls))
    result = TiingoProvider("dummy-key").get_history("600519.SS")
    assert result["ok"] is True
    assert len(calls) == 1
    assert "/tiingo/daily/600519.SS/prices" in calls[0]["url"]
