"""Checks on an LLM-written dossier thesis before it is stored (plan 2026-10-12, D7 / WP5).

The LLM writes words only. Of 180 prod theses (2026-10-12): 23 ended
mid-sentence (the grammar's ``maxLength`` forced the string closed), 4
leaked CJK tokens, several stated numbers that were in no input (VZ
"consensus 0.27", KMI "102x") or narrated "rate hikes" without any rate data,
and some contradicted their own direction. Each check below returns a short
reason code, stored as ``fallback_reason`` when the template is used:

* ``truncated``         — no full sentence is left after trimming;
* ``script``            — CJK, Hangul or Cyrillic characters;
* ``ungrounded_number`` — a number that is not in the fact sheet;
* ``macro``             — rate-direction words the macro facts do not support;
* ``contradiction``     — bearish wording on a ``long`` dossier;
* ``ticker``            — a near-miss of the candidate's own ticker ("DDDOG" for DDOG).

Pure: no DB, no network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

#: CJK unified ideographs (incl. ext. A), Hiragana/Katakana, Hangul syllables, Cyrillic.
_FOREIGN_SCRIPT = re.compile(r"[぀-ヿ㐀-鿿가-힯Ѐ-ӿ]")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
# A number with its unit: 12.5 %, 102x, €3.2bn, 0.27, -4.6 pp, 1,234.5
_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-+−])?(?P<cur>[€$£])?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s?(?P<unit>%|pp|bps?|x\b|×|bn|billion|m\b|mn|million|k\b)?",
    re.I,
)
_TIME_UNIT_AFTER = re.compile(r"^\s*-?\s*(months?|years?|quarters?|weeks?|days?|y\b|m\b)", re.I)
_HIKE_WORDS = re.compile(r"\brate hikes?\b|\brising (interest )?rates\b|\bhigher (interest )?rates\b|\btightening\b|"
                         r"\brate increases?\b", re.I)
_CUT_WORDS = re.compile(r"\brate cuts?\b|\bfalling (interest )?rates\b|\blower (interest )?rates\b|\beasing\b|"
                        r"\brate reductions?\b", re.I)
_CAPS_TOKEN = re.compile(r"\b[A-Z]{2,7}\b")
_BEARISH = re.compile(r"\bneutral stance\b|\bbearish\b|\bavoid\b|\bsell\b(?!-off)", re.I)

#: Absolute and relative tolerance of a grounded number (in the unit shown).
ABS_TOLERANCE = 0.15
REL_TOLERANCE = 0.02
#: A bare decimal below 1.5 (a ratio, a beta, "consensus 0.27") must match to rounding:
#: ±0.15 would let any ratio pass for any other.
SMALL_DECIMAL_TOLERANCE = 0.005


@dataclass
class ThesisCheck:
    """Outcome of :func:`check_thesis`; ``thesis``/``key_risks`` are the cleaned texts."""

    thesis: str
    key_risks: list[str]
    reason: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reason is None


def trim_to_sentence(text: str) -> str | None:
    """``text`` cut back to its last full sentence; None when no sentence is complete."""
    t = (text or "").strip()
    if not t:
        return None
    if t[-1] in ".!?" or t.endswith(('."', ".'", ".)")):
        return t
    ends = list(_SENTENCE_END.finditer(t))
    if not ends:
        return None
    cut = t[: ends[-1].end()].strip()
    return cut if len(cut) >= 8 else None


def has_foreign_script(text: str) -> bool:
    return bool(_FOREIGN_SCRIPT.search(text or ""))


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def fact_numbers(facts_text: str) -> list[float]:
    """Every number in the fact sheet, plus fractions shown as percentages (0.123 -> 12.3)."""
    values: list[float] = []
    for m in _NUMBER.finditer(facts_text or ""):
        v = _to_float(m.group("num"))
        if v is None:
            continue
        if m.group("sign") in ("-", "−"):
            v = -v
        values.append(v)
        if abs(v) <= 5.0:
            values.append(v * 100.0)
    return values


def _grounded(value: float, facts: list[float], abs_tolerance: float = ABS_TOLERANCE) -> bool:
    for f in facts:
        for candidate in (f, abs(f)):
            if abs(value - candidate) <= abs_tolerance or (
                candidate != 0 and abs(value - candidate) / abs(candidate) <= REL_TOLERANCE
            ):
                return True
    return False


def ungrounded_numbers(text: str, facts: list[float]) -> list[str]:
    """Numbers in ``text`` that match no fact (small time counts and years are allowed)."""
    bad: list[str] = []
    for m in _NUMBER.finditer(text or ""):
        value = _to_float(m.group("num"))
        if value is None:
            continue
        raw = m.group(0).strip()
        unit = (m.group("unit") or "").lower()
        if not unit and 1900 <= value <= 2100 and float(value).is_integer():
            continue  # a year
        if not unit and value <= 12 and float(value).is_integer() and _TIME_UNIT_AFTER.match(text[m.end():]):
            continue  # "6 months", "3 years"
        if not unit and value <= 10 and float(value).is_integer() and not m.group("cur"):
            continue  # counts in words ("2 risks", "top 3"): no claim about a figure
        tolerance = ABS_TOLERANCE if (unit in ("%", "pp") or value >= 1.5) else SMALL_DECIMAL_TOLERANCE
        if _grounded(abs(value), facts, tolerance):
            continue
        bad.append(raw)
    return bad


def claimed_rate_direction(text: str) -> str | None:
    hikes, cuts = bool(_HIKE_WORDS.search(text or "")), bool(_CUT_WORDS.search(text or ""))
    if hikes and cuts:
        return "mixed"
    return "hikes" if hikes else "cuts" if cuts else None


def macro_problem(text: str, macro: dict[str, Any] | None) -> str | None:
    """A problem when rate-direction words disagree with the macro block (or there is none)."""
    claimed = claimed_rate_direction(text)
    if claimed is None:
        return None
    directions = {d for d in ((macro or {}).get("rate_direction_12m") or {}).values() if d}
    if not directions:
        return f"says '{claimed}' but no rate data is given"
    if claimed == "mixed":
        return "mentions both rate hikes and rate cuts"
    if claimed not in directions:
        return f"says '{claimed}' but the 12-month policy-rate direction is {sorted(directions)}"
    return None


def contradiction_problem(text: str, direction: str) -> str | None:
    if direction != "long":
        return None
    m = _BEARISH.search(text or "")
    return f"'{m.group(0)}' contradicts a long stance" if m else None


def ticker_problem(text: str, symbol: str | None, facts_text: str) -> str | None:
    """A capitals token one letter off the candidate's ticker that the facts never show."""
    base = (symbol or "").split(".")[0].upper()
    if len(base) < 3:
        return None
    for token in _CAPS_TOKEN.findall(text or ""):
        if token == base or token in facts_text or abs(len(token) - len(base)) != 1:
            continue
        if base in token or token in base:
            return f"'{token}' is not the ticker; write {base}"
    return None


def check_thesis(
    thesis: str,
    key_risks: list[str],
    *,
    facts_text: str,
    macro: dict[str, Any] | None,
    direction: str,
    symbol: str | None = None,
) -> ThesisCheck:
    """Clean and check a thesis and its risks; the first failing rule names the reason."""
    trimmed = trim_to_sentence(thesis)
    risks = [r.strip() for r in key_risks if isinstance(r, str) and r.strip()]
    if trimmed is None:
        return ThesisCheck(thesis, risks, "truncated", ["the thesis has no complete sentence"])
    result = ThesisCheck(trimmed, risks)
    texts = [trimmed, *risks]
    if any(has_foreign_script(t) for t in texts):
        result.reason, result.problems = "script", ["non-Latin characters (CJK, Hangul or Cyrillic)"]
        return result
    facts = fact_numbers(facts_text)
    bad = [n for t in texts for n in ungrounded_numbers(t, facts)]
    if bad:
        result.reason, result.problems = "ungrounded_number", [f"numbers not in the facts: {', '.join(bad)}"]
        return result
    for t in texts:
        problem = macro_problem(t, macro)
        if problem:
            result.reason, result.problems = "macro", [problem]
            return result
    problem = contradiction_problem(trimmed, direction)
    if problem:
        result.reason, result.problems = "contradiction", [problem]
        return result
    for t in texts:
        problem = ticker_problem(t, symbol, facts_text)
        if problem:
            result.reason, result.problems = "ticker", [problem]
            return result
    return result
