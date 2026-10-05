"""Text sanitization helpers for LLM-generated content.

The goal is safe storage in UTF-8 databases and JSON payloads while
preserving all meaningful Unicode (curly quotes, currency symbols, umlauts,
em dashes, mathematical operators, etc.).  Only genuinely hazardous code
points are stripped:

* C0 control characters (U+0000–U+001F) **except** ``\\n`` (LF) and ``\\t`` (HT).
* ``\\r\\n`` sequences are normalized to ``\\n``; lone ``\\r`` (CR) is dropped.
* C1 control characters (U+0080–U+009F) — invisible, unused in modern text.
* Lone surrogates (U+D800–U+DFFF) — these are not valid Unicode scalar values
  and cause ``UnicodeEncodeError`` when writing to UTF-8 streams or databases.
"""
from __future__ import annotations

import re

# Matches any character that should be removed:
#   - \r\n  → replaced with \n  (handled before this pattern)
#   - lone \r → dropped
#   - C0 controls except \t and \n
#   - C1 controls U+0080–U+009F
#   - lone surrogates U+D800–U+DFFF
_STRIP_RE = re.compile(
    r"\r\n"                     # CRLF  → normalize to \n (substituted, not deleted)
    r"|[\r\x00-\x08\x0b-\x1f]" # lone CR + C0 controls except \t (0x09) and \n (0x0a)
    r"|[\x80-\x9f]"             # C1 controls
    r"|[\ud800-\udfff]",        # lone surrogates
    re.UNICODE,
)


def _replace_match(m: re.Match) -> str:  # type: ignore[type-arg]
    """Return the normalized replacement for each matched sequence."""
    if m.group(0) == "\r\n":
        return "\n"
    return ""


def ascii_safe(text: str | None) -> str:
    """Sanitize *text* for safe UTF-8 database and JSON storage.

    - ``None`` becomes an empty string.
    - All printable Unicode is preserved unchanged (curly quotes, €, ≤,
      umlauts, em dashes, arrows, etc.).
    - ``\\r\\n`` is normalized to ``\\n``; lone ``\\r`` is dropped.
    - C0 control characters (U+0000–U+001F) are stripped **except** ``\\n``
      and ``\\t``, which are preserved as meaningful whitespace.
    - C1 control characters (U+0080–U+009F) are stripped.
    - Lone surrogates (U+D800–U+DFFF) are stripped; they are not valid Unicode
      scalar values and crash UTF-8 encoders.

    The result is guaranteed to encode to UTF-8 without error.
    """
    if text is None:
        return ""
    return _STRIP_RE.sub(_replace_match, text)
