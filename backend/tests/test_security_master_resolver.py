"""Tests for the security-master resolver service (remediation task T2.2).

Contract under test: the evidence ladder resolves broker wire fragments to
canonical Securities once-per-instrument-ever. The DB is NEVER mocked (real
in-memory SQLite); every network seam (``requests.post``, ``yfinance.Ticker``)
is monkeypatched at the module boundary with call-counting fakes, so the
permanent-cache guarantee ("second resolve = zero HTTP") is provable.
"""

from __future__ import annotations

import difflib
import hashlib
import logging

import pytest
import requests
import yfinance
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import Security, SecurityAlias, SecurityListing
from app.foundation import security_master


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


ISIN = "IE00B4L5Y983"
WIRE_ALIAS = "TERED SHS USD (ACC) O.N."
SOURCE = "dkb_wire"

OPENFIGI_PAYLOAD = [
    {
        "data": [
            {
                "figi": "BBG000BLNNH6",
                "securityName": "iShares Core MSCI World UCITS ETF",
                "ticker": "IWDA",
                "exchCode": "XETR",
                "micCode": "XETR",
                "currency": "USD",
                "securityType": "ETF",
            }
        ]
    }
]


class _NetworkViolation(AssertionError):
    """Raised when un-mocked network code would leave the process."""


def _forbid_post(*args: object, **kwargs: object) -> object:
    raise _NetworkViolation("requests.post must be monkeypatched in tests")


def _forbid_ticker(*args: object, **kwargs: object) -> object:
    raise _NetworkViolation("yfinance.Ticker must be monkeypatched in tests")


@pytest.fixture(autouse=True)
def _hard_offline(monkeypatch: pytest.MonkeyPatch):
    """Default-deny both network seams; individual tests re-patch with fakes."""
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    monkeypatch.setattr(requests, "post", _forbid_post)
    monkeypatch.setattr(yfinance, "Ticker", _forbid_ticker)
    from app.foundation.providers import rate_limiter as rate_limiter_module

    rate_limiter_module._shared_limiters.pop(security_master.OPENFIGI_BATCH_LIMITER_KEY, None)
    yield
    rate_limiter_module._shared_limiters.pop(security_master.OPENFIGI_BATCH_LIMITER_KEY, None)


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._payload


class _PostRecorder:
    """Call-counting stand-in for requests.post at the module boundary."""

    def __init__(self, payloads: list[object]):
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    def __call__(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append(
            {"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")}
        )
        payload = self._payloads.pop(0) if self._payloads else [{"notFound": True}]
        return _FakeResponse(payload)


class _TickerRecorder:
    """Call-counting stand-in for yfinance.Ticker(symbol).info."""

    def __init__(self, infos: dict[str, dict]):
        self._infos = infos
        self.queries: list[str] = []

    def __call__(self, symbol: str) -> object:
        self.queries.append(symbol)

        class _Ticker:
            info = self._infos.get(symbol, {})

        return _Ticker()


def _seed_security(db, isin: str = ISIN, name: str | None = None) -> Security:
    security = Security(
        isin=isin,
        canonical_name=name or "iShares Core MSCI World UCITS ETF USD (Acc)",
        asset_type="etf",
    )
    db.add(security)
    db.commit()
    return security


# ---------------------------------------------------------------------------
# Rung 1 + 2: local evidence short-circuits before any network
# ---------------------------------------------------------------------------


def test_isin_hit_short_circuits_before_cache_and_network(monkeypatch):
    """An existing securities row wins immediately — zero HTTP, cache untouched."""
    db = _memory_db()
    seeded = _seed_security(db)
    post_recorder = _PostRecorder([])
    ticker_recorder = _TickerRecorder({})
    monkeypatch.setattr(requests, "post", post_recorder)
    monkeypatch.setattr(yfinance, "Ticker", ticker_recorder)

    resolved = security_master.resolve_security(
        db, isin=ISIN, alias=WIRE_ALIAS, alias_source=SOURCE
    )

    assert resolved is not None and resolved.id == seeded.id
    assert post_recorder.calls == []
    assert ticker_recorder.queries == []


def test_alias_cache_hit_returns_cached_security_with_zero_http(monkeypatch):
    """A persisted alias row resolves locally — no OpenFIGI, no Yahoo."""
    db = _memory_db()
    seeded = _seed_security(db)
    digest = hashlib.sha256(WIRE_ALIAS.encode("utf-8")).hexdigest()
    db.add(
        SecurityAlias(
            alias_sha256=digest,
            source=SOURCE,
            alias_value=WIRE_ALIAS,
            security_id=seeded.id,
        )
    )
    db.commit()
    post_recorder = _PostRecorder([])
    ticker_recorder = _TickerRecorder({})
    monkeypatch.setattr(requests, "post", post_recorder)
    monkeypatch.setattr(yfinance, "Ticker", ticker_recorder)

    resolved = security_master.resolve_security(
        db, alias=WIRE_ALIAS, alias_source=SOURCE
    )

    assert resolved is not None and resolved.id == seeded.id
    assert post_recorder.calls == []
    assert ticker_recorder.queries == []


# ---------------------------------------------------------------------------
# Rung 3: OpenFIGI secondary normaliser
# ---------------------------------------------------------------------------


def test_openfigi_success_upserts_rows_and_repeat_call_makes_zero_http(monkeypatch):
    """First call hits OpenFIGI once and persists Security+Listing+Alias; the
    second call for the same alias is served from the permanent cache."""
    db = _memory_db()
    recorder = _PostRecorder([OPENFIGI_PAYLOAD])
    monkeypatch.setattr(requests, "post", recorder)

    first = security_master.resolve_security(
        db, isin=ISIN, alias=WIRE_ALIAS, alias_source=SOURCE
    )
    assert first is not None
    assert first.isin == ISIN
    assert first.canonical_name == "iShares Core MSCI World UCITS ETF"
    assert first.asset_type == "stock"
    assert first.base_currency == "USD"
    assert first.ucits is True

    listing = db.query(SecurityListing).one()
    assert listing.security_id == first.id
    assert listing.mic == "XETR"
    assert listing.exchange_label == "XETRA"
    assert listing.symbol == "IWDA"
    assert listing.currency == "USD"
    assert listing.is_primary is True

    alias_row = db.query(SecurityAlias).one()
    assert alias_row.security_id == first.id
    assert alias_row.alias_value == WIRE_ALIAS
    assert alias_row.source == SOURCE

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == security_master.OPENFIGI_MAPPING_URL
    assert recorder.calls[0]["json"] == [
        {"idType": "ID_ISIN", "idValue": ISIN, "exchCode": "XETR"}
    ]

    second = security_master.resolve_security(
        db, isin=ISIN, alias=WIRE_ALIAS, alias_source=SOURCE
    )
    assert second is not None and second.id == first.id
    assert len(recorder.calls) == 1  # permanent cache: zero additional HTTP


def test_openfigi_retries_without_exch_code_when_xetr_empty(monkeypatch):
    """DEC-A: XETR first; an empty result retries WITHOUT the exchange code."""
    db = _memory_db()
    recorder = _PostRecorder([[], OPENFIGI_PAYLOAD])
    monkeypatch.setattr(requests, "post", recorder)

    resolved = security_master.resolve_security(
        db, isin=ISIN, alias=WIRE_ALIAS, alias_source=SOURCE
    )

    assert resolved is not None
    assert len(recorder.calls) == 2
    assert recorder.calls[0]["json"][0]["exchCode"] == "XETR"
    assert "exchCode" not in recorder.calls[1]["json"][0]
    assert db.query(Security).count() == 1


def test_openfigi_exception_returns_none_and_persists_no_partial_rows(monkeypatch):
    """Any network failure degrades cleanly — no Security/Listing/Alias rows."""
    db = _memory_db()
    calls: list[dict] = []

    def _boom(url: str, **kwargs: object) -> object:
        calls.append({"url": url})
        raise ConnectionError("openfigi down")

    monkeypatch.setattr(requests, "post", _boom)

    resolved = security_master.resolve_security(
        db, isin=ISIN, alias=WIRE_ALIAS, alias_source=SOURCE
    )

    assert resolved is None
    assert len(calls) == 1  # exception aborts the rung, no retry
    assert db.query(Security).count() == 0
    assert db.query(SecurityListing).count() == 0
    assert db.query(SecurityAlias).count() == 0


def test_openfigi_both_attempts_empty_returns_none_without_rows(monkeypatch):
    """Empty mapping results on both attempts resolve to nothing, cleanly."""
    db = _memory_db()
    recorder = _PostRecorder([[{"notFound": True}], []])
    monkeypatch.setattr(requests, "post", recorder)

    resolved = security_master.resolve_security(
        db, isin=ISIN, alias=WIRE_ALIAS, alias_source=SOURCE
    )

    assert resolved is None
    assert len(recorder.calls) == 2
    assert db.query(Security).count() == 0
    assert db.query(SecurityListing).count() == 0
    assert db.query(SecurityAlias).count() == 0


# ---------------------------------------------------------------------------
# Rung 4: Yahoo last resort (no ISIN) — suffix whitelist enforced
# ---------------------------------------------------------------------------


def test_yahoo_valid_suffix_accepted_and_persisted(monkeypatch):
    """A whitelisted suffix (.DE) resolves, upserts Security+Alias, and the
    repeat call is served from the permanent cache with zero extra lookups."""
    db = _memory_db()
    ticker_recorder = _TickerRecorder(
        {"SAP.DE": {"longName": "SAP SE", "symbol": "SAP.DE", "currency": "EUR"}}
    )
    post_recorder = _PostRecorder([])
    monkeypatch.setattr(yfinance, "Ticker", ticker_recorder)
    monkeypatch.setattr(requests, "post", post_recorder)

    first = security_master.resolve_security(
        db, alias="SAP.DE", alias_source="wire_ticker"
    )

    assert first is not None
    assert first.isin == "YF-SAP.DE"
    assert first.canonical_name == "SAP SE"
    assert first.asset_type == "stock"
    assert ticker_recorder.queries == ["SAP.DE"]
    assert post_recorder.calls == []
    assert db.query(SecurityAlias).one().security_id == first.id

    second = security_master.resolve_security(
        db, alias="SAP.DE", alias_source="wire_ticker"
    )
    assert second is not None and second.id == first.id
    assert ticker_recorder.queries == ["SAP.DE"]  # cached: no second lookup


def test_yahoo_bogus_suffix_rejected(monkeypatch):
    """An unknown suffix fails the whitelist (yfinance#2657) — no rows written."""
    db = _memory_db()
    ticker_recorder = _TickerRecorder(
        {"GARBAGE.XYZ": {"longName": "Junk Ltd", "symbol": "GARBAGE.XYZ"}}
    )
    monkeypatch.setattr(yfinance, "Ticker", ticker_recorder)

    resolved = security_master.resolve_security(
        db, alias="GARBAGE.XYZ", alias_source="wire_ticker"
    )

    assert resolved is None
    assert db.query(Security).count() == 0
    assert db.query(SecurityAlias).count() == 0


# ---------------------------------------------------------------------------
# Rung 5: fuzzy fallback against existing rows only
# ---------------------------------------------------------------------------


def test_fuzzy_auto_match_persists_alias(monkeypatch):
    """ratio >= 0.90 auto-resolves to the existing Security and caches the alias."""
    db = _memory_db()
    seeded = _seed_security(db)
    ticker_recorder = _TickerRecorder({})  # Yahoo yields nothing (empty info)
    monkeypatch.setattr(yfinance, "Ticker", ticker_recorder)

    alias = "iShares Core MSCI World UCITS ETF USD"
    target = security_master.normalize_alias(seeded.canonical_name)
    ratio = difflib.SequenceMatcher(
        None, security_master.normalize_alias(alias), target
    ).ratio()
    assert ratio >= 0.90  # fixture precondition

    resolved = security_master.resolve_security(
        db, alias=alias, alias_source=SOURCE
    )

    assert resolved is not None and resolved.id == seeded.id
    assert db.query(SecurityAlias).one().security_id == seeded.id


def test_fuzzy_mid_band_logs_review_warning_and_writes_nothing(caplog, monkeypatch):
    """0.75 <= ratio < 0.90 returns None, logs candidates, persists nothing."""
    db = _memory_db()
    _seed_security(db, isin="US0378331005", name="Apple Inc.")
    ticker_recorder = _TickerRecorder({})
    monkeypatch.setattr(yfinance, "Ticker", ticker_recorder)

    alias = "Apple Corp."
    ratio = difflib.SequenceMatcher(
        None,
        security_master.normalize_alias(alias),
        security_master.normalize_alias("Apple Inc."),
    ).ratio()
    assert 0.75 <= ratio < 0.90  # fixture precondition

    with caplog.at_level(logging.WARNING, logger="app.foundation.security_master"):
        resolved = security_master.resolve_security(
            db, alias=alias, alias_source=SOURCE
        )

    assert resolved is None
    assert "Apple Inc." in caplog.text
    assert db.query(SecurityAlias).count() == 0
    assert db.query(Security).count() == 1


def test_fuzzy_below_threshold_silent_none(monkeypatch, caplog):
    """ratio < 0.75 stays silent: None, no writes, no review warning."""
    db = _memory_db()
    _seed_security(db, isin="US0378331005", name="Apple Inc.")
    ticker_recorder = _TickerRecorder({})
    monkeypatch.setattr(yfinance, "Ticker", ticker_recorder)

    with caplog.at_level(logging.WARNING, logger="app.foundation.security_master"):
        resolved = security_master.resolve_security(
            db, alias="Zzzq Wxxv Holdings Unlimited", alias_source=SOURCE
        )

    assert resolved is None
    assert caplog.text == ""
    assert db.query(SecurityAlias).count() == 0


# ---------------------------------------------------------------------------
# OpenFIGI v2->v3 regression + resolve_securities_batch
# ---------------------------------------------------------------------------


def _openfigi_entry(symbol: str) -> dict:
    return {
        "data": [
            {
                "securityName": f"Company {symbol}",
                "ticker": symbol,
                "exchCode": "XETR",
                "micCode": "XETR",
                "currency": "USD",
            }
        ]
    }


def test_openfigi_v3_url_and_header_name(monkeypatch):
    """Regression: the mapping URL is v3 (v2 sunset 2026-06-30) and the API-key
    header is the documented ``X-OPENFIGI-APIKEY``, not ``X-OpenFIGI-API``."""
    db = _memory_db()
    monkeypatch.setenv("OPENFIGI_API_KEY", "test-key-123")
    recorder = _PostRecorder([OPENFIGI_PAYLOAD])
    monkeypatch.setattr(requests, "post", recorder)

    resolved = security_master.resolve_security(db, isin=ISIN, alias_source=SOURCE)

    assert resolved is not None
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["url"] == "https://api.openfigi.com/v3/mapping"
    assert recorder.calls[0]["headers"]["X-OPENFIGI-APIKEY"] == "test-key-123"
    assert "X-OpenFIGI-API" not in recorder.calls[0]["headers"]


def test_resolve_securities_batch_issues_one_post_per_batch_not_per_candidate(monkeypatch):
    """N candidates with ISINs, batch size 3 -> ceil(N/3) POSTs, not N."""
    db = _memory_db()
    monkeypatch.setattr(security_master, "_OPENFIGI_BATCH_SIZE_NO_KEY", 3)

    n = 7
    candidates = [(f"SYM{i}", f"US000000000{i}") for i in range(n)]
    call_payloads = [
        [_openfigi_entry(sym) for sym, _isin in candidates[start : start + 3]]
        for start in range(0, n, 3)
    ]
    recorder = _PostRecorder(call_payloads)
    monkeypatch.setattr(requests, "post", recorder)

    security_master.resolve_securities_batch(db, candidates, alias_source="discover_pipeline")

    assert len(recorder.calls) == -(-n // 3)  # ceil division, no external import
    assert db.query(Security).count() == n


def test_resolve_securities_batch_stops_gracefully_when_rate_limited(monkeypatch):
    """A limiter exhausted mid-batch stops issuing POSTs; unresolved candidates
    are simply left unresolved this run, not raised or blocked on."""
    db = _memory_db()
    monkeypatch.setattr(security_master, "_OPENFIGI_BATCH_SIZE_NO_KEY", 2)
    monkeypatch.setattr(
        security_master,
        "_OPENFIGI_RATE_LIMIT_NO_KEY",
        security_master.RateLimitConfig(max_calls=1, window_seconds=60, description="test"),
    )

    candidates = [(f"SYM{i}", f"US000000000{i}") for i in range(6)]
    recorder = _PostRecorder([[_openfigi_entry("SYM0")]])
    monkeypatch.setattr(requests, "post", recorder)

    security_master.resolve_securities_batch(db, candidates, alias_source="discover_pipeline")

    assert len(recorder.calls) == 1  # only the first batch got a token
    assert db.query(Security).count() == 1  # first batch's one ISIN resolved


def test_resolve_securities_batch_skips_local_hits_with_zero_http(monkeypatch):
    """A candidate already resolvable via rungs 1-2 (ISIN row / alias cache)
    never becomes an OpenFIGI job."""
    db = _memory_db()
    seeded = _seed_security(db)
    post_recorder = _PostRecorder([])
    monkeypatch.setattr(requests, "post", post_recorder)

    security_master.resolve_securities_batch(
        db, [("EXISTING", ISIN)], alias_source="discover_pipeline"
    )

    assert post_recorder.calls == []
    assert db.query(SecurityAlias).one().security_id == seeded.id


def test_resolve_securities_batch_ignores_candidates_without_isin(monkeypatch):
    """No ISIN -> no OpenFIGI job possible; batch resolution is a no-op for it
    (single-item Yahoo/fuzzy rungs are resolve_security's job, not the batch's)."""
    db = _memory_db()
    post_recorder = _PostRecorder([])
    monkeypatch.setattr(requests, "post", post_recorder)

    security_master.resolve_securities_batch(
        db, [("NOISIN", None)], alias_source="discover_pipeline"
    )

    assert post_recorder.calls == []
    assert db.query(Security).count() == 0
