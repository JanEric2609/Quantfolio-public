"""Utility helpers for DKB FinTS integration.

Extracted from the monolithic dkb.py for maintainability. Contains pure
functions, logging capture, and sanitisation routines -- no DB access,
no class instances, no state.
"""

import hashlib
import logging
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator
from urllib.parse import urlparse

_FINTS_SAFE_SCHEME_HOST = "https://fints.dkb.de"
_FINTS_DEFAULT_URL = "https://fints.dkb.de/fints"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ASCII safety (LANG=C crash defence)
# ---------------------------------------------------------------------------

def _ascii_safe(text: str | None, placeholder: str = "--") -> str:
    """Replace non-ASCII characters that crash PostgreSQL when LANG=C.

    Use this for all strings that will be persisted to a database Text column
    or sent over the wire in environments that may not have a UTF-8 locale
    configured.

    Strategy -- layered defense:
      1. Replace known-problematic Unicode punctuation (em dash, en dash)
         with the ASCII *placeholder* before touching anything else, so the
         common case reads cleanly (``--`` rather than ``?``).
      2. Re-encode as ASCII with ``errors="replace"``, turning any remaining
         non-ASCII (U+0080+) into ``?`` so the result is guaranteed pure ASCII.
    """
    if text is None:
        return ""
    result = text.replace("\u2014", placeholder).replace("\u2013", placeholder)
    return result.encode("ascii", errors="replace").decode("ascii")


# ---------------------------------------------------------------------------
# Transaction hashing (deduplication)
# ---------------------------------------------------------------------------

def dkb_transaction_hash(tx_date: date, amount: Decimal, reference: str) -> str:
    canonical = (
        f"{tx_date.isoformat()}|{Decimal(amount).quantize(Decimal('0.01'))}|"
        f"{' '.join(reference.split()).lower()}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Generic FinTS parsing helpers
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "_asdict"):
        return value._asdict()
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in ("", None):
            return mapping[key]
    return None


def _list_from_any(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in ("", None):
        return None
    return _decimal_from_any(value)


def _decimal_from_any(value: Any) -> Decimal:
    if value in ("", None):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    raw = str(value).strip().replace("\xa0", " ")
    raw = raw.replace("EUR", "").replace("\u20ac", "").strip()
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    raw = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
    return Decimal(raw or "0")


def _date_from_any(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _coerce_int(value: Any) -> int | None:
    """Return ``int(value)`` if the value is a non-None integer-like thing, else ``None``."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Error sanitisation
# ---------------------------------------------------------------------------

def _sanitize_fints_error(message: str, *secrets: str | None) -> str:
    """Strip any credential substrings out of a FinTS error before it is
    surfaced to the UI or persisted. FinTS exceptions don't normally embed the
    PIN/username, but we redact defensively so a verbose upstream message can
    never leak them."""
    cleaned = (message or "").strip()
    for secret in secrets:
        if secret and len(str(secret)) >= 3:
            cleaned = cleaned.replace(str(secret), "***")
    return cleaned


# IBANs (printed or compact) and balances/amounts: FinTS DEBUG wire logging carries
# both in clear (HISPA account lists, HISAL balances, MT940 statement lines).
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
# German/FinTS decimal-comma amounts: 1234,56  1.234,56  EUR1234,56
_AMOUNT_COMMA_RE = re.compile(r"(?<![\d.,])\d[\d.]*,\d+(?!\d)")
# Decimal('1234.56') reprs and "1234.56 EUR" amounts from parsed objects.
_DECIMAL_REPR_RE = re.compile(r"Decimal\('-?[\d.]+'\)")
_AMOUNT_DOT_CURRENCY_RE = re.compile(r"-?\d+\.\d{2}(?= ?(?:EUR|USD|GBP|CHF)\b)")


def _mask_financial_data(message: str) -> str:
    """Mask IBANs (keep country + last 4) and amounts in a log line.

    The PIN and username are replaced by exact value in ``_sanitize_fints_error``;
    account numbers and balances have no known value to match, so they are
    recognised by shape.
    """

    def _iban(match: re.Match[str]) -> str:
        compact = match.group(0).replace(" ", "")
        return f"{compact[:2]}**...{compact[-4:]}"

    masked = _IBAN_RE.sub(_iban, message)
    masked = _DECIMAL_REPR_RE.sub("Decimal('***')", masked)
    masked = _AMOUNT_DOT_CURRENCY_RE.sub("***", masked)
    return _AMOUNT_COMMA_RE.sub("***", masked)


# ---------------------------------------------------------------------------
# Log capture
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    """Logging handler that appends formatted+sanitized records to a list."""

    def __init__(self, sink: list[str], *secrets: str | None) -> None:
        super().__init__()
        self._sink = sink
        self._secrets = secrets
        self.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            self._sink.append(_mask_financial_data(_sanitize_fints_error(line, *self._secrets)))
        except Exception:
            pass


@contextmanager
def _capture_dkb_logs(debug_fints_logging: bool, *secrets: str | None) -> Iterator[list[str]]:
    """Capture log output into a list, sanitizing secrets from every line.

    ``app.foundation.dkb`` INFO lines are always captured (TAN mechanism
    selection, account enumeration, decision flow). ``fints`` DEBUG lines (raw
    wire messages carrying FinTS segment codes) are additionally captured only
    when *debug_fints_logging* is ``True``. Logger levels and handlers are
    restored on exit even if the wrapped code raises.
    """
    captured: list[str] = []
    handler = _ListHandler(captured, *secrets)
    handler.setLevel(logging.DEBUG)

    app_logger = logging.getLogger("app.foundation.dkb")
    app_orig_level = app_logger.level
    if not app_logger.level or app_logger.level > logging.INFO:
        app_logger.setLevel(logging.INFO)
    app_logger.addHandler(handler)

    fints_logger = logging.getLogger("fints")
    fints_orig_level = fints_logger.level
    fints_orig_propagate = fints_logger.propagate
    if debug_fints_logging:
        if not fints_logger.level or fints_logger.level > logging.DEBUG:
            fints_logger.setLevel(logging.DEBUG)
        fints_logger.addHandler(handler)
        # Raw wire lines go only to the masking handler above. Propagating them
        # would also write the unmasked lines (IBANs, balances) to the root
        # handlers, i.e. the service journal.
        fints_logger.propagate = False

    try:
        yield captured
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(app_orig_level)
        if debug_fints_logging:
            fints_logger.removeHandler(handler)
            fints_logger.setLevel(fints_orig_level)
            fints_logger.propagate = fints_orig_propagate


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------

def _safe_fints_url(url: str | None) -> str:
    """Clamp a user-supplied FinTS URL to https://fints.dkb.de/<path>.

    Preserves the path component so the canonical /fints suffix can be
    overridden in tests/staging, but pins scheme+host to prevent the
    decrypted PIN from being sent to an attacker-controlled host.
    """
    if not url:
        return _FINTS_DEFAULT_URL
    try:
        parsed = urlparse(url)
        safe_prefix = _FINTS_SAFE_SCHEME_HOST.rstrip("/")
        actual_prefix = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if actual_prefix == safe_prefix:
            return url
    except Exception:
        pass
    logger.warning(
        "DKB FinTS URL %r does not match allowed host %s -- clamping to safe default.",
        url, _FINTS_SAFE_SCHEME_HOST,
    )
    return _FINTS_DEFAULT_URL
