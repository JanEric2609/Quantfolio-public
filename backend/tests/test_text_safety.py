"""Tests for app.foundation.text_safety.ascii_safe (UTF-8 preserving contract)."""
from __future__ import annotations

import pytest

from app.foundation.text_safety import ascii_safe


# ---------------------------------------------------------------------------
# Preservation: printable Unicode must survive unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("char,label", [
    ("“", "left double curly quote"),
    ("”", "right double curly quote"),
    ("‘", "left single curly quote"),
    ("’", "right single curly quote"),
    ("€", "euro sign"),
    ("≤", "less-than-or-equal"),
    ("\xe4", "a-umlaut"),
    ("\xf6", "o-umlaut"),
    ("\xfc", "u-umlaut"),
    ("—", "em dash"),
    ("–", "en dash"),
    ("→", "rightwards arrow"),
])
def test_printable_unicode_preserved(char: str, label: str) -> None:
    result = ascii_safe(f"before {char} after")
    assert char in result, f"{label!r} was unexpectedly stripped"


# ---------------------------------------------------------------------------
# Control character stripping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("char,label", [
    ("\x00", "NUL"),
    ("\x1b", "ESC"),
    ("\x07", "BEL"),
    ("\x01", "SOH"),
    ("\x1f", "US"),
])
def test_c0_controls_stripped(char: str, label: str) -> None:
    result = ascii_safe(f"a{char}b")
    assert char not in result, f"{label!r} was not stripped"
    assert result == "ab", f"Expected 'ab', got {result!r}"


@pytest.mark.parametrize("char,label", [
    ("\x80", "PAD / U+0080"),
    ("\x9f", "APC / U+009F"),
    ("\x85", "NEL / U+0085"),
])
def test_c1_controls_stripped(char: str, label: str) -> None:
    result = ascii_safe(f"x{char}y")
    assert char not in result, f"{label!r} was not stripped"
    assert result == "xy"


# ---------------------------------------------------------------------------
# Whitespace normalisation
# ---------------------------------------------------------------------------

def test_newline_preserved() -> None:
    assert ascii_safe("line1\nline2") == "line1\nline2"


def test_tab_preserved() -> None:
    assert ascii_safe("col1\tcol2") == "col1\tcol2"


def test_crlf_normalized_to_lf() -> None:
    assert ascii_safe("line1\r\nline2") == "line1\nline2"


def test_lone_cr_dropped() -> None:
    assert ascii_safe("a\rb") == "ab"


# ---------------------------------------------------------------------------
# Surrogate handling
# ---------------------------------------------------------------------------

def test_lone_surrogate_removed() -> None:
    text = "a" + "\ud800" + "b"
    result = ascii_safe(text)
    assert "\ud800" not in result
    assert result == "ab"


def test_result_with_surrogate_encodes_utf8() -> None:
    text = "hello \ud83d world"  # lone high surrogate
    result = ascii_safe(text)
    # Must not raise
    encoded = result.encode("utf-8")
    assert b"hello" in encoded


# ---------------------------------------------------------------------------
# None input
# ---------------------------------------------------------------------------

def test_none_returns_empty_string() -> None:
    assert ascii_safe(None) == ""


# ---------------------------------------------------------------------------
# Round-trip: every output must encode to UTF-8 without error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "plain ascii",
    "em dash — and euro €",
    "äöü ÄÖÜ ß",  # German umlauts + sharp s
    "‘curly’ “quotes”",
    "mixed \x00 control \x1b chars \ud800 surrogates",
    None,
])
def test_result_always_utf8_encodable(text: str | None) -> None:
    result = ascii_safe(text)
    # Must not raise UnicodeEncodeError
    result.encode("utf-8")
