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
* ``ticker``            — a near-miss of the candidate's own ticker ("DDDOG" for DDOG);
* ``mislabel``          — a true figure under the wrong name, or a claim the data does
  not carry ("3-month momentum" for the 12-1 month figure, "strong momentum" at
  the 55th percentile, a raw internal code). All 15 theses of the 2026-11-05 run
  passed the checks above, and several of them read like this (Discover audit,
  2026-11-06).

A risk that only says a data feed is missing (``ml_signal_unavailable``, "ML
signal unavailable") is dropped from the risks: it is no risk of the company.

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

_INTERNAL_CODE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
#: Field names the model copies from its facts. The figure beside one is true, so
#: a plain name is enough; rejecting the text for it left 4 of 5 funds on the
#: template in run 4b6f5ebf (2026-11-07). Any other code is still rejected.
_CODE_WORDS = {
    "momentum_rank": "momentum rank",
    "momentum_12_1m": "12-1 month momentum",
    "trailing_1m_annualized_return": "trailing 1-month annualized return",
    "trailing_3y_annualized_return": "trailing 3-year annualized return",
    "market_beta_weekly": "weekly market beta",
    "market_beta": "market beta",
    "volatility_6m": "6-month volatility",
    "max_drawdown": "maximum drawdown",
    "fit_score": "portfolio fit score",
    "debt_equity": "debt-to-equity",
    "de_ratio": "debt-to-equity",
    "revenue_growth_pct": "revenue growth",
    "revenue_growth": "revenue growth",
    "pe_ratio": "P/E",
    "pb_ratio": "P/B",
    "forward_pe": "forward P/E",
    "analyst_upside": "analyst upside",
    "analyst_target": "analyst target",
    "insider_buyers_30d": "insiders buying in the last 30 days",
}
_KNOWN_CODE = re.compile(r"\b(?:" + "|".join(sorted(_CODE_WORDS, key=len, reverse=True)) + r")\b")
_DATA_FEEDS = (
    r"(?:ml|machine[- ]learning) (?:signal|model)|alpha ?crafter|validated (?:alpha )?factors?|alpha factors?|"
    r"insider (?:data|signal)|estimate data|sentiment data|analyst data"
)
_DATA_GAP_RISK = re.compile(
    rf"\b(?:{_DATA_FEEDS})\b[^.]*\b(?:unavailable|missing|absent|not available)\b|"
    rf"\b(?:unavailable|missing|absent|lack of|absence of|no)\b[^.]*\b(?:{_DATA_FEEDS})\b",
    re.I,
)
# (?<!\d-) leaves "12-1 month momentum", the one momentum figure the facts carry.
_WRONG_MOMENTUM_PERIOD = re.compile(
    r"(?<!\d-)\b(?:\d{1,2}|one|three|six|twelve)[- ]months?\s+(?:\w+\s+)?momentum\b|"
    r"\b(?:12|twelve)[- ]month\s+(?:gain|rise|increase|return)\b",
    re.I,
)
_DRAWDOWN_AS_LIMIT = re.compile(r"\bdrawdown\b[^.]{0,40}\b(?:capped|limited to|cannot exceed|floor)\b", re.I)
_RATIO_AS_RETURN = re.compile(r"\breturns?\s+of\s+[-+−]?\d?\.\d+\b(?!\s*%)", re.I)
_BETA_AS_CORRELATION = re.compile(
    r"\bbeta\b[^.]{0,60}\b(?:suggests?|indicates?|shows?|implies?)\b[^.]{0,30}\bcorrelat", re.I,
)
_MARKET_IMPLIED_ESTIMATE = re.compile(r"\bmarket[- ]implied\b", re.I)
#: A low beta over a past window is no protection, and the facts carry no stress test.
_DOWNSIDE_PROTECTION = re.compile(r"\bdownside protection\b", re.I)
#: No validated factors feed the dossier; "low alpha factors" describes missing data.
_ALPHA_FACTORS = re.compile(r"\balpha factors?\b", re.I)
_REGIME_AFFINITY = re.compile(r"\bregime affinity\b", re.I)
_HORIZON_NONSENSE = re.compile(r"\bexceed\w*\s+the\s+\d+-month horizon\b", re.I)
_STRONG_MOMENTUM = re.compile(r"\b(?:strong|robust|exceptional|high)\b[^.]{0,20}\bmomentum\b", re.I)
# "Diversified" describes what is inside a fund or a company, which can be true at
# any correlation; only a claim about the portfolio needs a low one.
_DIVERSIFIER_CLAIM = re.compile(
    r"(?<!little )(?<!limited )(?<!no )(?<!not )(?<!low )(?<!poor )(?<!minimal )(?<!reduced )(?<!less )\bdiversif(?:ier|iers|ication|ying|ies|y)\b", re.I,
)
#: "High correlation" or "substantial overlap" with the portfolio, the mirror of a diversifier claim.
_HIGH_OVERLAP_CLAIM = re.compile(
    r"\b(?:high|substantial|significant|strong|heavy|considerable)\b[^.]{0,30}\b(?:correlation|overlap)\b"
    r"(?=[^.]*\b(?:portfolio|holdings|book)\b)|"
    r"\b(?:portfolio|holdings|book)\b[^.]*\b(?:high|substantial|significant|strong|heavy|considerable)\b"
    r"[^.]{0,30}\b(?:correlation|overlap)\b",
    re.I,
)
_INSIDER_CLUSTER = re.compile(r"\binsiders?\b[^.]{0,30}\bcluster|\bcluster\b[^.]{0,20}\b(?:buy|buys|buying|insiders?)\b", re.I)
_HIGH_DEBT = re.compile(r"\b(?:high|elevated|heavy|significant)\b[^.]{0,25}\b(?:debt|leverage)\b", re.I)
_DEBT_EQUITY_LINE = re.compile(r"\bdebt[- ](?:to[- ])?equity\b|\bdebt ratio\b|\bd/e\b|\bleverage\b", re.I)
_SCORE_AS_RISK = re.compile(r"\b(?:fundamentals?|composite|portfolio fit|fit|momentum|analyst) scores?\b", re.I)
_HOLDINGS_CLAIM = re.compile(r"\bholdings?\b[^.]*", re.I)
_NOT_A_HOLDING = frozenset({"ETF", "ETC", "MSCI", "UCITS", "USD", "EUR", "GBP", "FTSE", "ESG"})

# Run 57c8f8b8 (2026-11-10), checked against the web: wording the figures
# could not catch. Each such sentence or risk line is dropped, not the dossier.
#: A company named as an example ("holdings like Moderna"): IUSN has no holdings data.
_NAMED_EXAMPLE = re.compile(r"\b(?:like|such as|including|e\.g\.)\s+([A-Z][\w&'-]+(?:\s+[A-Z][\w&'-]+)*)")
#: The owner's portfolio described by a style nothing measured ("a growth style portfolio",
#: "a technology-heavy portfolio").
_PORTFOLIO_STYLE = re.compile(
    r"\b[\w-]+[- ](?:heavy|style|tilted|oriented|focused|dominated|driven|centric|weighted)\s+portfolio\b|"
    r"\b(?:growth|value|tech|technology|defensive|cyclical|income)\s+portfolio\b",
    re.I,
)
_PORTFOLIO_STYLE_CLAUSE = re.compile(
    r"\s*\b(?:within|in|for|across|to)\s+(?:an?|the|your)\s+(?:" + _PORTFOLIO_STYLE.pattern + r")", re.I,
)
#: The cash return without the system's estimate it is compared with ("an annualized return
#: exceeding 1.22 % from cash", "trades above cash yields of ... from cash").
_CASH_PHRASE = re.compile(r"\bfrom cash over the same months\b", re.I)
_ESTIMATE_PHRASE = re.compile(r"\(the system's estimate\)", re.I)
#: The stance justified by the broad-market regime ("aligns with the sideways regime model").
_REGIME_JUSTIFICATION = re.compile(
    r"\b(?:align\w*|consistent|fits?|suits?|match\w*|support\w*|in line)\b[^.]{0,40}\bregime\b|"
    r"\bregime\b[^.]{0,40}\b(?:supports?|favou?rs?|suits?)\b",
    re.I,
)
_CORRELATION_WORD = re.compile(r"\bcorrelat", re.I)
_OTHER_RISK_FIGURE = re.compile(r"\bbeta\b|\blargest fall\b|\bdrawdown\b|\bvolatility\b", re.I)
#: Insider buying is not a risk to a long position (ADM listed one buyer as its first risk).
_INSIDER_BUY_RISK = re.compile(r"\binsiders?\b[^.]*\b(?:bought|buys?|buying|purchas\w*)\b", re.I)
_INSIDER_SELL = re.compile(r"\b(?:sold|sells?|selling|sales?)\b", re.I)
#: A beta below this is not "market sensitivity" (ENX's 0.29 was listed as a risk).
LOW_BETA_RISK_MAX = 0.8
_BETA_VALUE = re.compile(r"\bbeta of (-?\d+(?:\.\d+)?)", re.I)
#: Two figure phrases run together without a conjunction ("a ratio of 15.3 a return on equity").
_RUN_TOGETHER = re.compile(r"(\d(?:[\d.,]*\d)?(?:\s?%)?)\s+(an?\s+(?:return|trailing|market|weekly|debt|revenue|annual|largest|past|12-1))")


def _named_example_unknown(text: str, facts_text: str) -> bool:
    for m in _NAMED_EXAMPLE.finditer(text):
        name = m.group(1).split()[0]
        singular = name[:-1] if name.endswith("s") else name  # "such as ETFs"
        if name not in _NOT_A_HOLDING and singular not in _NOT_A_HOLDING and not re.search(rf"\b{re.escape(name)}\b", facts_text):
            return True
    return False


def sloppy_sentence(sentence: str, facts_text: str = "") -> str | None:
    """Why a thesis sentence says something the data does not, or None."""
    if _named_example_unknown(sentence, facts_text):
        return "names a company that is not in the data"
    style = _PORTFOLIO_STYLE.search(sentence)
    if style and style.group(0).lower() not in facts_text.lower():
        return "describes the portfolio's style, which nothing measured"
    if _CASH_PHRASE.search(sentence) and not _ESTIMATE_PHRASE.search(sentence):
        return "quotes the cash return without the estimate it is compared with"
    if _REGIME_JUSTIFICATION.search(sentence):
        return "justifies the stance by the broad-market regime"
    if (
        _DIVERSIFIER_CLAIM.search(sentence)
        and _OTHER_RISK_FIGURE.search(sentence)
        and not _CORRELATION_WORD.search(sentence)
    ):
        # ADM: "diversification benefits given its modest market beta ... and a largest fall of 19 %".
        return "argues diversification from beta or a fall, not the correlation"
    return None


def sloppy_risk(risk: str, facts_text: str = "") -> str | None:
    """Why a risk line is not a risk the data shows, or None."""
    if _INSIDER_BUY_RISK.search(risk) and not _INSIDER_SELL.search(risk):
        return "insider buying is not a risk"
    m = _BETA_VALUE.search(risk)
    if m and abs(float(m.group(1))) < LOW_BETA_RISK_MAX and not re.search(r"\b(?:volatil|drawdown|fall)\w*", risk, re.I):
        return "a low market beta is not a risk"
    return sloppy_sentence(risk, facts_text)


def tidy_sentence(sentence: str) -> str:
    """Mend two slips the model makes around the figure phrases.

    A verdict said twice: "adds little diversification given a weekly correlation of
    0.96 with the portfolio (it adds little diversification)" (SPYY). Two phrases with
    no "and" between them: "a price-to-earnings ratio of 15.3 a return on equity of 9.1 %" (ABN).
    """
    # From the end backwards: cutting a later match leaves the earlier offsets valid.
    for m in reversed(list(re.finditer(r"\s*\((?:it )?([^()]{6,60})\)", sentence))):
        inner = m.group(1).strip().lower()
        before = sentence[: m.start()].lower()
        if inner in before:
            sentence = sentence[: m.start()] + sentence[m.end():]
    # "within a technology-heavy portfolio": the clause goes, the figures stay.
    sentence = _PORTFOLIO_STYLE_CLAUSE.sub("", sentence)
    # "(it adds little diversification) to existing positions" (XDEM): the phrase already names the portfolio.
    sentence = re.sub(r"\)\s+to (?:the |its |your )?existing (?:positions|holdings)\b", ")", sentence)
    return _RUN_TOGETHER.sub(r"\1 and \2", sentence)


def distinct_risks(risks: list[str]) -> list[str]:
    """Risk lines less repeats: the same text, or the same figures said again (XDEM listed its
    correlation of 0.82 twice)."""
    seen_text: set[str] = set()
    seen_figures: set[frozenset[str]] = set()
    out: list[str] = []
    for risk in risks:
        key = re.sub(r"\W+", " ", risk.lower()).strip()
        figures = frozenset(re.findall(r"\d+(?:\.\d+)?", risk))
        if key in seen_text or (figures and figures in seen_figures):
            continue
        seen_text.add(key)
        if figures:
            seen_figures.add(figures)
        out.append(risk)
    return out


#: "Strong momentum" needs at least this rank; below it, the figure speaks for itself.
STRONG_MOMENTUM_RANK = 0.8
#: A weekly correlation with the book from here on is no diversifier.
DIVERSIFIER_MAX_CORRELATION = 0.5
#: Debt-to-equity (in percent) from which debt can be called high: debt equals equity.
HIGH_DEBT_EQUITY_PCT = 100.0
#: A cluster of insiders takes more than one buyer.
MIN_CLUSTER_BUYERS = 2
#: Shorter than this, what is left of a thesis after a removal is no thesis.
MIN_KEPT_THESIS_CHARS = 40

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
        # A whole word of the facts, not a substring: "DDO" sits inside "DDOG".
        if token == base or re.search(rf"\b{re.escape(token)}\b", facts_text) or abs(len(token) - len(base)) != 1:
            continue
        if base in token or token in base:
            return f"'{token}' is not the ticker; write {base}"
    return None


def is_data_gap_risk(risk: str) -> bool:
    """A "risk" that only reports a missing data feed or carries a raw internal code."""
    return bool(_INTERNAL_CODE.search(risk) or _DATA_GAP_RISK.search(risk))


_FUND_STYLE_CLAIM = re.compile(
    r"\b(?:growth|value|dividend|quality|momentum) (?:exposure|tilt|bias)\b|"
    r"\b(?:large|mid|small)[- ]cap (?:US |U\.S\. |European |global )?(?:technology|tech|growth|value)\b",
    re.I,
)


def mislabel_problems(
    text: str,
    facts_text: str = "",
    *,
    momentum_rank: float | None = None,
    correlation: float | None = None,
    is_fund: bool = False,
    de_ratio: float | None = None,
    insider_buyers: float | None = None,
) -> list[str]:
    """True figures under the wrong name, and claims the data does not carry."""
    problems: list[str] = []
    fixed_rules = (
        (_INTERNAL_CODE, "'{}' is an internal code; say it in words or leave it out"),
        (_WRONG_MOMENTUM_PERIOD, "'{}': the only momentum figure given is 12-1 month; call it that"),
        (_DRAWDOWN_AS_LIMIT, "'{}': a past drawdown is what happened, not a limit"),
        (_RATIO_AS_RETURN, "'{}': that figure is a ratio or correlation, not a return"),
        (_BETA_AS_CORRELATION, "'{}': beta is not correlation; they are given separately"),
        (_MARKET_IMPLIED_ESTIMATE, "'{}': the estimate is cash plus beta times the equity premium plus a small "
                                   "share of the trailing return; do not call it market-implied"),
        (_REGIME_AFFINITY, "'{}' does not separate candidates; leave it out"),
        (_HORIZON_NONSENSE, "'{}' has no meaning; name the actual risk"),
        (_DOWNSIDE_PROTECTION, "'{}' is not in the data; a low beta over a past window protects nothing"),
        (_ALPHA_FACTORS, "'{}': no validated factors are given; leave them out"),
    )
    for pattern, message in fixed_rules:
        m = pattern.search(text)
        if m:
            problems.append(message.format(m.group(0)))
    m = _STRONG_MOMENTUM.search(text)
    if m and momentum_rank is not None and momentum_rank < STRONG_MOMENTUM_RANK:
        problems.append(f"'{m.group(0)}' overstates a momentum rank of {momentum_rank:.2f}")
    if correlation is not None and correlation >= DIVERSIFIER_MAX_CORRELATION and _DIVERSIFIER_CLAIM.search(text):
        problems.append(f"a diversification claim needs a low correlation with the portfolio; it is {correlation:.2f}")
    if correlation is not None and correlation < DIVERSIFIER_MAX_CORRELATION and _HIGH_OVERLAP_CLAIM.search(text):
        problems.append(f"a correlation of {correlation:.2f} with the portfolio is not high")
    if de_ratio is not None and de_ratio < HIGH_DEBT_EQUITY_PCT and _HIGH_DEBT.search(text):
        problems.append(f"debt-to-equity of {de_ratio:.1f} % is not high")
    if insider_buyers is not None and insider_buyers < MIN_CLUSTER_BUYERS and _INSIDER_CLUSTER.search(text):
        problems.append(f"{int(insider_buyers)} insider(s) bought; that is not a cluster")
    if is_fund:
        # SPYY: "growth exposure" and "large-cap US technology" with no holdings data behind them.
        for m in _FUND_STYLE_CLAIM.finditer(text):
            if m.group(0).lower() not in facts_text.lower():
                problems.append(f"'{m.group(0)}': the fund's style or sector mix is not in the data given")
        for claim in _HOLDINGS_CLAIM.findall(text):
            for token in _CAPS_TOKEN.findall(claim):
                if token not in _NOT_A_HOLDING and not re.search(rf"\b{re.escape(token)}\b", facts_text):
                    problems.append(f"holding '{token}' is not in the holdings given")
    return problems


def plain_field_names(text: str) -> str:
    """``text`` with the known field names in plain words."""
    return _KNOWN_CODE.sub(lambda m: _CODE_WORDS[m.group(0)], text)


def _unsupported_claim(text: str, correlation: float | None) -> bool:
    """A claim the data cannot carry, whatever else the sentence says."""
    if _MARKET_IMPLIED_ESTIMATE.search(text) or _DOWNSIDE_PROTECTION.search(text) or _ALPHA_FACTORS.search(text):
        return True
    if correlation is not None and correlation < DIVERSIFIER_MAX_CORRELATION and _HIGH_OVERLAP_CLAIM.search(text):
        return True
    return (
        correlation is not None
        and correlation >= DIVERSIFIER_MAX_CORRELATION
        and bool(_DIVERSIFIER_CLAIM.search(text))
    )


def without_unsupported_sentences(thesis: str, correlation: float | None = None, facts_text: str | None = None) -> str:
    """``thesis`` less the sentences with an unsupported claim; unchanged if too little is left.

    In run dd3fe7b9 (2026-11-07) the model wrote a diversification claim on both
    attempts for 3 of 5 funds, and their true figures went with it to the template.
    A thesis of nothing but such claims stays as it is and is rejected by the checks.
    """
    sentences = re.split(r"(?<=[.!?])\s+", thesis.strip())
    # Tidied first: a style clause ("within a growth style portfolio") is cut, not
    # the sentence with its figures.
    tidied = [tidy_sentence(s) for s in sentences]
    kept = " ".join(
        s for s in tidied
        if not _unsupported_claim(s, correlation)
        and (facts_text is None or sloppy_sentence(s, facts_text) is None)
    )
    return kept if len(kept) >= MIN_KEPT_THESIS_CHARS else thesis


def is_false_risk(risk: str, de_ratio: float | None = None) -> bool:
    """A line that is not a risk: a score of ours, or ordinary debt presented as one."""
    if _SCORE_AS_RISK.search(risk):
        return True
    return de_ratio is not None and de_ratio < HIGH_DEBT_EQUITY_PCT and bool(_DEBT_EQUITY_LINE.search(risk))


def check_thesis(
    thesis: str,
    key_risks: list[str],
    *,
    facts_text: str,
    macro: dict[str, Any] | None,
    direction: str,
    symbol: str | None = None,
    momentum_rank: float | None = None,
    correlation: float | None = None,
    is_fund: bool = False,
    de_ratio: float | None = None,
    insider_buyers: float | None = None,
) -> ThesisCheck:
    """Clean and check a thesis and its risks; the first failing rule names the reason."""
    trimmed = trim_to_sentence(plain_field_names(thesis or ""))
    named = [plain_field_names(r.strip()) for r in key_risks if isinstance(r, str) and r.strip()]
    risks = distinct_risks([
        r for r in (tidy_sentence(x) for x in named)
        if not is_data_gap_risk(r) and not is_false_risk(r, de_ratio) and not _unsupported_claim(r, correlation)
        and sloppy_risk(r, facts_text) is None
    ])
    if trimmed is None:
        return ThesisCheck(thesis, risks, "truncated", ["the thesis has no complete sentence"])
    trimmed = without_unsupported_sentences(trimmed, correlation, facts_text)
    result = ThesisCheck(trimmed, risks)
    # Left in only because too little would remain without it (ADM: both sentences).
    sloppy = [w for w in (sloppy_sentence(x, facts_text) for x in re.split(r"(?<=[.!?])\s+", trimmed)) if w]
    if sloppy:
        result.reason, result.problems = "mislabel", [f"the thesis {w}" for w in sloppy]
        return result
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
    problems = [
        p for t in texts
        for p in mislabel_problems(
            t, facts_text, momentum_rank=momentum_rank, correlation=correlation, is_fund=is_fund,
            de_ratio=de_ratio, insider_buyers=insider_buyers,
        )
    ]
    if problems:
        result.reason, result.problems = "mislabel", problems
    return result
