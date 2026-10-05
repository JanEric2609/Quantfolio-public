"""Security-master resolver: map broker aliases/ISINs to canonical Securities.

Remediation task T2.2 (docs/archive/audits/2026-08-universe-regime-audit.md). DKB FinTS
wire fragments such as ``"TERED SHS USD (ACC) O.N."`` for IE00B4L5Y983 must
become resolvable once-per-instrument-ever: every successful resolution is
written to ``security_aliases`` (keyed by sha256(normalized alias) + source),
so the same wire string never triggers a second external lookup (Gemini-recon
lesson: permanent-cache everything resolved).

Evidence ladder (stop at the first hit):

1. ISIN direct — an existing ``securities`` row.
2. Alias cache — ``security_aliases`` lookup by (source, sha256).
3. OpenFIGI mapping (only with an ISIN) — XETRA-first per DEC-A, then one
   retry without the exchange code. Upserts Security + SecurityListing rows.
4. Yahoo (only without an ISIN) — last resort; candidate symbols must pass a
   conservative suffix whitelist (the yfinance#2657 lesson: never trust
   unverified external identifiers).
5. Fuzzy fallback against EXISTING securities rows only (never network):
   stdlib ``difflib.SequenceMatcher`` — a deliberate deviation from the audit
   report's rapidfuzz mention so this module stays dependency-free. Ratios
   >=0.90 auto-resolve; 0.75–0.89 logs a review warning and writes nothing;
   below 0.75 stays silent.

Every external call is best-effort: any network or parse failure is logged at
DEBUG and degrades to the next rung — the resolver never raises.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
from typing import Any

import requests
from sqlalchemy.orm import Session

from app.foundation.models.entities import Security, SecurityAlias, SecurityListing
from app.foundation.providers.rate_limiter import RateLimitConfig, get_shared_limiter

logger = logging.getLogger(__name__)

OPENFIGI_MAPPING_URL = "https://api.openfigi.com/v3/mapping"
#: DEC-A: XETRA is the canonical venue, so the first mapping attempt pins XETR.
OPENFIGI_PRIMARY_EXCH_CODE = "XETR"

#: Best-effort MIC -> human label for ``exchange_label`` on listings.
_EXCHANGE_LABELS: dict[str, str] = {
    "XETR": "XETRA",
    "XNAS": "NASDAQ",
    "XNYS": "NYSE",
    "XLON": "London Stock Exchange",
    "XAMS": "Euronext Amsterdam",
    "XPAR": "Euronext Paris",
    "XMIL": "Borsa Italiana",
    "XMAD": "BME Madrid",
    "XSWX": "SIX Swiss",
    "XHEL": "Nasdaq Helsinki",
}

#: Conservative Yahoo suffix whitelist (yfinance#2657 lesson). A candidate
#: symbol is accepted only when it is a bare ticker (US convention) or ends in
#: a known Yahoo exchange suffix; anything else is rejected as unverified.
_YAHOO_SUFFIX_WHITELIST: frozenset[str] = frozenset({
    ".DE", ".L", ".AS", ".PA", ".MC", ".MI", ".SW", ".VI", ".BR", ".CO",
    ".HE", ".IR", ".OL", ".ST", ".AX", ".TO", ".V", ".NZ", ".HK", ".T",
    ".SS", ".SZ", ".KS", ".KQ", ".TW", ".SI", ".JK", ".SA", ".MX", ".SG",
})

_WS_RE = re.compile(r"\s+")

#: OpenFIGI v3 documented batch/rate limits (openfigi.com/api/documentation):
#: 10 mapping jobs/request unauthenticated, 100/request with an API key;
#: 25 requests/min unauthenticated, 25 requests/6s with a key. The limiter
#: configs below stay a bit under the documented ceiling as headroom.
_OPENFIGI_BATCH_SIZE_NO_KEY = 10
_OPENFIGI_BATCH_SIZE_WITH_KEY = 100

_OPENFIGI_RATE_LIMIT_NO_KEY = RateLimitConfig(
    max_calls=20, window_seconds=60,
    description="OpenFIGI v3 mapping, unauthenticated: documented 25/min",
)
_OPENFIGI_RATE_LIMIT_WITH_KEY = RateLimitConfig(
    max_calls=20, window_seconds=6,
    description="OpenFIGI v3 mapping, API key: documented 25/6s",
)
#: Shared-limiter key for :func:`resolve_securities_batch`'s OpenFIGI POSTs
#: (exposed so tests can reset the module-level shared limiter between runs).
OPENFIGI_BATCH_LIMITER_KEY = "openfigi_mapping"


def normalize_alias(alias: str) -> str:
    """Normalise an alias for hashing/comparison: strip, collapse spaces, upper."""
    return _WS_RE.sub(" ", alias.strip()).upper()


def _alias_sha256(alias: str) -> str:
    """Permanent-cache key for *alias*: sha256 of the normalised form."""
    return hashlib.sha256(normalize_alias(alias).encode("utf-8")).hexdigest()


def _remember_alias(
    db: Session,
    security: Security,
    alias: str | None,
    alias_source: str,
) -> None:
    """Upsert the permanent alias->security cache row (idempotent)."""
    if not alias or not alias.strip():
        return
    digest = _alias_sha256(alias)
    row = (
        db.query(SecurityAlias)
        .filter(
            SecurityAlias.source == alias_source[:24],
            SecurityAlias.alias_sha256 == digest,
        )
        .first()
    )
    if row is None:
        db.add(
            SecurityAlias(
                alias_sha256=digest,
                source=alias_source[:24],
                alias_value=alias,
                security_id=security.id,
            )
        )
    else:
        row.security_id = security.id
        row.alias_value = alias
    db.flush()


def _openfigi_headers(api_key: str | None) -> dict[str, str]:
    """Common OpenFIGI v3 request headers; adds the auth header when a key is set.

    Header name is ``X-OPENFIGI-APIKEY`` per the v3 docs
    (openfigi.com/api/documentation) -- not ``X-OpenFIGI-API``.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    return headers


def _openfigi_map_isin(isin: str) -> list[dict[str, Any]] | None:
    """Query OpenFIGI v3 mapping for *isin*; XETRA-first per DEC-A.

    Returns the flattened ``data`` entry list, or None when both attempts come
    back empty or anything fails. Kept tiny and private so tests can
    monkeypatch it directly instead of stubbing HTTP.
    """
    headers = _openfigi_headers(os.environ.get("OPENFIGI_API_KEY"))
    for exch_code in (OPENFIGI_PRIMARY_EXCH_CODE, None):
        body: dict[str, Any] = {"idType": "ID_ISIN", "idValue": isin}
        if exch_code is not None:
            body["exchCode"] = exch_code
        try:
            response = requests.post(
                OPENFIGI_MAPPING_URL, json=[body], headers=headers, timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.debug("OpenFIGI mapping failed for %s", isin, exc_info=True)
            return None
        if isinstance(payload, list) and payload:
            entries = payload[0].get("data")
            if entries:
                return entries
        logger.debug("OpenFIGI returned no data for %s (exchCode=%s)", isin, exch_code)
    return None


def _upsert_openfigi_security(
    db: Session,
    isin: str,
    entries: list[dict[str, Any]],
    alias: str | None,
    alias_source: str,
) -> Security:
    """Upsert a Security plus one SecurityListing per FIGI entry from OpenFIGI."""
    first = entries[0] if entries else {}
    name = (first.get("securityName") or first.get("name") or "").strip() or isin
    currency = (first.get("currency") or "").strip().upper() or "EUR"

    security = db.query(Security).filter(Security.isin == isin).first()
    if security is None:
        security = Security(
            isin=isin,
            canonical_name=name[:240],
            asset_type="stock",
            base_currency=currency,
            ucits="UCITS" in name.upper(),
            provider_meta_json=json.dumps({"provider": "openfigi"}),
        )
        db.add(security)
        db.flush()

    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        raw_mic = entry.get("micCode") or entry.get("exchCode") or "XXXX"
        mic = raw_mic.strip().upper() or "XXXX"
        symbol = (entry.get("ticker") or isin).strip() or isin
        key = (mic, symbol)
        if key in seen:
            continue
        seen.add(key)
        exists = (
            db.query(SecurityListing)
            .filter(
                SecurityListing.security_id == security.id,
                SecurityListing.mic == mic,
                SecurityListing.symbol == symbol,
            )
            .first()
        )
        if exists is not None:
            continue
        listing_currency = (entry.get("currency") or currency).strip().upper()
        db.add(
            SecurityListing(
                security_id=security.id,
                mic=mic,
                exchange_label=_EXCHANGE_LABELS.get(mic, mic)[:32],
                symbol=symbol[:32],
                currency=listing_currency[:3],
                is_primary=index == 0,
            )
        )
    db.flush()
    _remember_alias(db, security, alias, alias_source)
    return security


def _yahoo_symbol_allowed(symbol: str) -> bool:
    """Conservative suffix gate encoding the yfinance#2657 lesson."""
    cleaned = symbol.strip().upper()
    if not cleaned:
        return False
    if "." not in cleaned:
        return True  # bare ticker — US convention
    return any(cleaned.endswith(suffix) for suffix in _YAHOO_SUFFIX_WHITELIST)


def _resolve_via_yahoo(db: Session, alias: str, alias_source: str) -> Security | None:
    """Last-resort Yahoo resolution when no ISIN is available.

    On success upserts a Security keyed by a ``YF-<symbol>`` placeholder (the
    schema's natural key is the ISIN column and a Yahoo-only instrument has
    none) plus the permanent alias row. Any failure degrades to None.
    """
    try:
        import yfinance as yf

        info = yf.Ticker(alias).info or {}
    except Exception:
        logger.debug("Yahoo lookup failed for %r", alias, exc_info=True)
        return None

    name = (info.get("longName") or info.get("shortName") or "").strip()
    symbol = (info.get("symbol") or "").strip()
    if not name or not symbol:
        return None
    if not _yahoo_symbol_allowed(symbol):
        logger.warning(
            "Yahoo candidate symbol %r for alias %r rejected: suffix fails whitelist",
            symbol,
            alias,
        )
        return None

    natural_key = f"YF-{symbol.upper()}"[:12]
    security = db.query(Security).filter(Security.isin == natural_key).first()
    if security is None:
        security = Security(
            isin=natural_key,
            canonical_name=name[:240],
            asset_type="stock",
            base_currency=(info.get("currency") or "EUR").strip().upper()[:3],
            ucits="UCITS" in name.upper(),
            provider_meta_json=json.dumps({"provider": "yahoo", "yahoo_symbol": symbol}),
        )
        db.add(security)
        db.flush()
    _remember_alias(db, security, alias, alias_source)
    return security


def _fuzzy_resolve(db: Session, alias: str, alias_source: str) -> Security | None:
    """Difflib fallback against EXISTING securities rows only (never network).

    >=0.90 auto-resolves (+persists the alias); 0.75–0.89 logs a review warning
    naming the candidates and writes nothing; <0.75 stays silent.
    """
    target = normalize_alias(alias)
    if not target:
        return None

    symbols_by_security: dict[str, list[str]] = {}
    for listing in db.query(SecurityListing).all():
        symbols_by_security.setdefault(listing.security_id, []).append(listing.symbol)

    best: Security | None = None
    best_ratio = 0.0
    review: list[str] = []
    for security in db.query(Security).all():
        fields = [normalize_alias(security.canonical_name)]
        fields.extend(
            normalize_alias(s) for s in symbols_by_security.get(security.id, [])
        )
        ratios = [
            difflib.SequenceMatcher(None, target, field).ratio()
            for field in fields
            if field
        ]
        ratio = max(ratios, default=0.0)
        if ratio > best_ratio:
            best, best_ratio = security, ratio
        if 0.75 <= ratio < 0.90:
            review.append(f"{security.canonical_name!r} [{security.isin}] {ratio:.2f}")

    if best is not None and best_ratio >= 0.90:
        _remember_alias(db, best, alias, alias_source)
        return best
    if review:
        logger.warning(
            "Alias %r unresolved; near-matches need manual review: %s",
            alias,
            "; ".join(review),
        )
    return None


def _resolve_local(
    db: Session,
    normalized_isin: str | None,
    alias: str | None,
    alias_source: str,
) -> Security | None:
    """Rungs 1-2 only: ISIN direct hit, then the permanent alias cache.

    DB-only, no network -- shared by :func:`resolve_security` and
    :func:`resolve_securities_batch` so both single-item and bulk callers get
    the same free, no-HTTP short-circuit before anything hits OpenFIGI/Yahoo.
    """
    # Rung 1 — ISIN direct hit against existing rows.
    if normalized_isin:
        security = (
            db.query(Security).filter(Security.isin == normalized_isin).first()
        )
        if security is not None:
            _remember_alias(db, security, alias, alias_source)
            return security

    # Rung 2 — permanent alias cache.
    if alias:
        cached = (
            db.query(SecurityAlias)
            .filter(
                SecurityAlias.source == alias_source[:24],
                SecurityAlias.alias_sha256 == _alias_sha256(alias),
            )
            .first()
        )
        if cached is not None:
            security = db.get(Security, cached.security_id)
            if security is not None:
                return security
    return None


def _openfigi_map_isins_batch(
    isins: list[str], *, api_key: str | None, exch_code: str | None
) -> dict[str, list[dict[str, Any]]]:
    """POST one OpenFIGI v3 batch mapping request for *isins* (already capped
    to the caller's batch size: 10 unauthenticated, 100 with an API key).

    Returns ``{isin: entries}`` for ISINs that came back with data; an ISIN
    missing from the result got no hit this attempt. Never raises -- a hard
    failure yields an empty dict, the same degrade-to-next-rung contract as
    the single-item :func:`_openfigi_map_isin`.
    """
    headers = _openfigi_headers(api_key)
    jobs: list[dict[str, Any]] = []
    for isin in isins:
        job: dict[str, Any] = {"idType": "ID_ISIN", "idValue": isin}
        if exch_code is not None:
            job["exchCode"] = exch_code
        jobs.append(job)
    try:
        response = requests.post(
            OPENFIGI_MAPPING_URL, json=jobs, headers=headers, timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logger.debug("OpenFIGI batch mapping failed for %d ISINs", len(isins), exc_info=True)
        return {}
    if not isinstance(payload, list):
        return {}
    hits: dict[str, list[dict[str, Any]]] = {}
    for isin, job_result in zip(isins, payload):
        if isinstance(job_result, dict):
            entries = job_result.get("data")
            if entries:
                hits[isin] = entries
    return hits


def resolve_securities_batch(
    db: Session,
    candidates: list[tuple[str, str | None]],
    *,
    alias_source: str,
) -> None:
    """Best-effort bulk resolution of (symbol, isin) pairs into the security master.

    Same "no opinion on failure" contract as :func:`resolve_security` --
    never raises, writes what it can. Built for Discover's per-run candidate
    list: rungs 1-2 (DB-only, via :func:`_resolve_local`) run for every
    candidate first; OpenFIGI misses that carry an ISIN are then resolved via
    batched POSTs (<=10 jobs/request unauthenticated, <=100 with
    ``OPENFIGI_API_KEY``) against a shared rate limiter, instead of one HTTP
    call per candidate. Candidates with no ISIN at all are not resolved here
    (no OpenFIGI job possible) -- they still fall through to
    :func:`resolve_security`'s single-item Yahoo/fuzzy rungs elsewhere, this
    function is OpenFIGI-batching only.
    """
    api_key = os.environ.get("OPENFIGI_API_KEY")
    batch_size = _OPENFIGI_BATCH_SIZE_WITH_KEY if api_key else _OPENFIGI_BATCH_SIZE_NO_KEY
    limiter = get_shared_limiter(
        OPENFIGI_BATCH_LIMITER_KEY,
        _OPENFIGI_RATE_LIMIT_WITH_KEY if api_key else _OPENFIGI_RATE_LIMIT_NO_KEY,
    )

    pending: dict[str, list[str]] = {}
    for symbol, isin in candidates:
        normalized_isin = (isin or "").strip().upper() or None
        security = _resolve_local(db, normalized_isin, symbol, alias_source)
        if security is not None or normalized_isin is None:
            continue
        pending.setdefault(normalized_isin, []).append(symbol)

    remaining = list(pending.keys())
    for exch_code in (OPENFIGI_PRIMARY_EXCH_CODE, None):
        if not remaining:
            break
        still_pending = remaining
        remaining = []
        for start in range(0, len(still_pending), batch_size):
            batch = still_pending[start : start + batch_size]
            if limiter.acquire() is None:
                skipped = len(still_pending) - start
                logger.debug(
                    "resolve_securities_batch: OpenFIGI rate limit reached, "
                    "%d ISIN(s) left unresolved this run",
                    skipped,
                )
                return
            hits = _openfigi_map_isins_batch(batch, api_key=api_key, exch_code=exch_code)
            for isin in batch:
                entries = hits.get(isin)
                if not entries:
                    remaining.append(isin)
                    continue
                aliases = pending[isin]
                security = _upsert_openfigi_security(
                    db, isin, entries, aliases[0], alias_source
                )
                for extra_alias in aliases[1:]:
                    _remember_alias(db, security, extra_alias, alias_source)


def resolve_security(
    db: Session,
    *,
    isin: str | None = None,
    alias: str | None = None,
    alias_source: str = "unknown",
) -> Security | None:
    """Resolve an ISIN and/or broker alias to a canonical :class:`Security`.

    Walks the evidence ladder documented in the module docstring, stopping at
    the first hit. Every resolution (including direct-ISIN hits) refreshes the
    permanent alias cache when an *alias* is supplied, honouring the
    once-per-instrument-ever contract. Returns None when every applicable rung
    misses; never raises.
    """
    normalized_isin = (isin or "").strip().upper() or None

    security = _resolve_local(db, normalized_isin, alias, alias_source)
    if security is not None:
        return security

    # Rung 3 — OpenFIGI secondary normaliser (needs an ISIN).
    if normalized_isin:
        entries = _openfigi_map_isin(normalized_isin)
        if entries:
            return _upsert_openfigi_security(
                db, normalized_isin, entries, alias, alias_source
            )

    # Rung 4 — Yahoo last resort (only without an ISIN).
    if alias and not normalized_isin:
        security = _resolve_via_yahoo(db, alias, alias_source)
        if security is not None:
            return security

    # Rung 5 — fuzzy match against existing rows only.
    if alias:
        return _fuzzy_resolve(db, alias, alias_source)
    return None
