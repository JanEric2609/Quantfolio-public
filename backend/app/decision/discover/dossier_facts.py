"""The dossier's figures, written by code: keyed phrases the model cites as ``{key}``.

Why (plan 2026-11-08, WP4). Every dossier rejected since 2026-11-05 failed on a
figure or on the word for one: TRV invented "-$68.8 million", then called a
momentum rank of 0.79 "strong" twice against the rule that says strong starts at
0.80. Each run added a rule to the prompt and a pattern to ``thesis_checks``.
Template/neural hybrids remove the class instead of chasing it: the model chooses
what to say and in what order, code writes every number and the judgement word
that goes with it (TempLM, Zhang et al. 2022: unfaithful outputs 83 % -> 0 %; the
content-selection / realisation split of Moryossef et al. 2019). LLMs map numbers
to words inconsistently, so the descriptor bins are fixed here, not left to the
model.

A fact's phrase is a noun phrase that reads mid-sentence: "TRV has {momentum}".
The model sees the phrases, cites them by key and writes no digit itself;
:func:`render` substitutes the phrases and drops any sentence that writes a
figure of its own or cites a key that does not exist. The existing
``thesis_checks.check_thesis`` then still runs on the rendered text.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.decision.discover.thesis_checks import DIVERSIFIER_MAX_CORRELATION, STRONG_MOMENTUM_RANK
from app.foundation.fundamentals_context import growth_basis_text

#: A cited fact: ``{momentum}``.
SLOT = re.compile(r"\{([a-z0-9_]+)\}")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_DIGIT = re.compile(r"\d")
#: Figures the text may name without a key: the momentum convention's own name.
_FIXED_TERMS = ("12-1",)


@dataclass(frozen=True)
class Fact:
    key: str
    text: str


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def momentum_descriptor(rank: float) -> str:
    """Where a momentum rank sits among the run's candidates, in fixed bins."""
    if rank >= STRONG_MOMENTUM_RANK:
        return "among the strongest of this run's candidates"
    if rank >= 0.6:
        return "above the middle of this run's candidates"
    if rank >= 0.4:
        return "around the middle of this run's candidates"
    return "below the middle of this run's candidates"


def _pct(value: float, digits: int = 1, sign: bool = True) -> str:
    return f"{value:+.{digits}f} %" if sign else f"{value:.{digits}f} %"


def _usd(value: float) -> str:
    amount = abs(value)
    if amount >= 1e9:
        return f"{amount / 1e9:.1f} billion US dollars"
    if amount >= 1e6:
        return f"{amount / 1e6:.1f} million US dollars"
    return f"{amount:,.0f} US dollars"


def build_facts(
    scores: dict[str, Any],
    *,
    annual: dict[str, Any],
    expected_return_pct: float | None,
    horizon_months: int,
    fundamentals: dict[str, Any],
    macro: dict[str, Any] | None,
    cash_pct: float | None = None,
    earnings_text: str | None = None,
    earnings_near: bool = False,
    is_fund: bool = False,
    recent_fall_limit: float = 0.20,
    filings_text: str | None = None,
    corporate_action_text: str | None = None,
) -> list[Fact]:
    """The candidate's figures as keyed phrases, in the order a reader needs them.

    Measured figures come first and the analyst target after them: price targets
    have run well above where prices ended (Bradshaw, Brown and Huang 2013).
    """
    facts: list[Fact] = []

    def add(key: str, text: str | None) -> None:
        if text:
            facts.append(Fact(key, text))

    mom = _d(scores.get("momentum_quality"))
    backtest = _d(scores.get("backtest_vs_benchmark"))
    quant = _d(scores.get("quant_signals"))
    fit = _d(scores.get("portfolio_fit"))
    sf = _d(scores.get("sentiment_fundamentals"))
    insider = _d(scores.get("insider_signal"))

    add("horizon", f"{horizon_months} months")
    if (er := _num(expected_return_pct)) is not None:
        add("expected_return", f"{_pct(er)} over {horizon_months} months (the system's estimate)")
    if (cash := _num(cash_pct)) is not None:
        add("cash_return", f"{_pct(cash, 2, sign=False)} from cash over the same months")

    if (m := _num(mom.get("momentum_12_1m"))) is not None:
        rank = _num(mom.get("momentum_rank"))
        text = f"a 12-1 month price return of {_pct(m * 100)}"
        notes = [momentum_descriptor(rank)] if rank is not None else []
        # What the 12-1 return does not show goes into the same phrase, so citing
        # momentum cannot leave it out (the model ignored a separate instruction).
        gain, share = _num(mom.get("largest_day_return")), _num(mom.get("largest_day_share"))
        if mom.get("jump_driven") is True and gain is not None and share is not None:
            notes.append(
                f"though one day, {mom.get('largest_day_date')} at {_pct(gain * 100)}, gave {share * 100:.0f} % of "
                "it, and momentum from one jump tends not to continue"
            )
        if (fall := _num(mom.get("fall_from_3m_high"))) is not None and fall >= recent_fall_limit:
            notes.append(f"the latest close is {fall * 100:.1f} % below the highest close of the last 3 months")
        add("momentum", text + (f" ({'; '.join(notes)})" if notes else ""))
    # The verification gate's volatility (13 months), the figure the risk lines and
    # the 100 % ceiling use; the 6-month one is the fallback. One figure per dossier.
    gate = _d(scores.get("verification_gate"))
    if (vol := _num(gate.get("volatility"))) is not None:
        add("volatility", f"an annual volatility of {vol * 100:.0f} % (last 13 months)")
    elif (vol := _num(mom.get("volatility_6m"))) is not None:
        add("volatility", f"an annual volatility of {vol * 100:.0f} % (last 6 months)")
    if (mdd := _num(mom.get("max_drawdown"))) is not None:
        add("drawdown", f"a largest fall of {abs(mdd) * 100:.0f} % from a peak (last 13 months)")

    kind = annual.get("anchor_kind")
    anchor = _num(_d(annual.get("components")).get("anchor_pct"))
    if kind == "trailing_3y_annualized_return" and anchor is not None:
        years = _num(backtest.get("candidate_return_years"))
        span = f"{years:.1f} years" if years is not None else "3 years"
        add("past_return", f"a past return of {_pct(anchor)} a year (last {span}; not a forecast)")
    benchmark = backtest.get("benchmark") or quant.get("benchmark")
    if (excess := _num(backtest.get("excess_return_annual"))) is not None and benchmark:
        years = _num(backtest.get("candidate_return_years"))
        span = f"{years:.1f} years" if years is not None else "3 years"
        add("excess_return", f"a return of {_pct(excess * 100)} a year against {benchmark} (last {span})")

    beta_w, beta_d = _num(quant.get("market_beta_weekly")), _num(quant.get("market_beta"))
    if beta_w is not None or beta_d is not None:
        against = quant.get("benchmark") or "its benchmark"
        basis = "weekly, 2 years" if beta_w is not None else "daily, 1 year"
        add("beta", f"a market beta of {(beta_w if beta_w is not None else beta_d):.2f} against {against} ({basis})")

    if (corr := _num(fit.get("correlation"))) is not None:
        # The high case says "little diversification": thesis_checks reads any other
        # form of "diversif..." at a correlation of 0.5 or more as a claim.
        verdict = (
            "low enough to diversify it" if corr < DIVERSIFIER_MAX_CORRELATION
            else "it adds little diversification"
        )
        add("correlation", f"a weekly correlation of {corr:.2f} with the portfolio ({verdict})")

    # A split or spin-off after the last report: the ratios below are blanked by
    # the caller, and the reader is told why they are missing (corporate_action.py).
    add("corporate_action", corporate_action_text)
    if not is_fund:
        if (pe := _num(fundamentals.get("pe"))) is not None and pe > 0:
            add("pe", f"a trailing price-to-earnings ratio of {pe:.1f}")
        roe = fundamentals.get("roe")
        if (roe_v := _num(roe)) is not None:
            add("roe", f"a return on equity of {roe_v * 100:.1f} %")
        if (de := _num(fundamentals.get("de_ratio"))) is not None and de >= 0:
            level = "less debt than equity" if de < 100 else "more debt than equity"
            add("debt_equity", f"debt at {de:.0f} % of equity ({level})")
        if (rg := _num(fundamentals.get("revenue_growth"))) is not None:
            # "On the year" read as annual growth; yfinance's figure is one quarter.
            basis = growth_basis_text(fundamentals.get("growth_basis"))
            add("revenue_growth", f"revenue growth of {_pct(rg * 100)} {basis}")

    buyers = _num(insider.get("cluster_buy_score"))
    if buyers is not None and buyers >= 1:
        add(
            "insider_buying",
            "one insider buying in the last 30 days, one buyer and not a cluster" if buyers < 2
            else f"{int(buyers)} insiders buying in the last 30 days",
        )
    if (flow := _num(insider.get("net_insider_flow_usd"))) is not None and flow != 0:
        add("insider_flow", f"net insider {'selling' if flow < 0 else 'buying'} of {_usd(flow)}")

    # The company's own 8-K filings of the last 30 days (restatement, auditor
    # change, delisting notice, impairment, departures): sec_8k.py.
    add("filings", filings_text)

    if earnings_text:
        text = earnings_text.rstrip(".")
        if not earnings_near:
            text = text[:-1] + "; too far off to be a key risk)" if text.endswith(")") else text + " (too far off to be a key risk)"
        add("earnings", text)

    if sf.get("analyst_target") is not None and (upside := _num(sf.get("analyst_upside"))) is not None:
        count = sf.get("analyst_count")
        who = f"{int(count)} analysts" if isinstance(count, (int, float)) else "analysts"
        side = "above" if upside >= 0 else "below"
        add(
            "analyst_target",
            f"an analyst median price target {abs(upside) * 100:.1f} % {side} the current price ({who}; such "
            "targets have tended to sit above where prices ended)",
        )

    macro = _d(macro)
    ecb = _d(macro.get("ecb_deposit_rate"))
    if (lvl := _num(ecb.get("level_pct"))) is not None:
        add("ecb_rate", f"an ECB deposit rate of {lvl:.2f} % ({ecb.get('direction_12m') or 'no change'} over 12 months)")
    fed = _d(macro.get("fed_funds_upper"))
    if (lvl := _num(fed.get("level_pct"))) is not None:
        add("fed_rate", f"a Fed funds upper bound of {lvl:.2f} % ({fed.get('direction_12m') or 'no change'} over 12 months)")
    hicp = _d(macro.get("hicp_euro_area"))
    if (v := _num(hicp.get("value_pct"))) is not None:
        add("euro_inflation", f"euro-area inflation of {v:.1f} % a year in {hicp.get('month')}")
    return facts


def facts_prompt(facts: list[Fact]) -> str:
    lines = "".join(f"{{{f.key}}}: {f.text}\n" for f in facts)
    return (
        "Facts (cite a figure only by writing its key in braces, e.g. {momentum}; the key is replaced by "
        "the text after it, which already carries the right judgement word):\n" + lines
    )


SLOT_RULES = (
    "Rules for thesis and key_risks:\n"
    "- thesis: at most 3 complete sentences and 450 characters; end with a full stop.\n"
    "- Write no digits at all. Every figure appears only as a key from the Facts list. A key stands for a whole "
    "noun phrase with its own article, period and judgement, so write it bare and do not repeat what it says "
    "around it. Example: \"TRV has {momentum} and {correlation}. It trades at {pe}. The system expects "
    "{expected_return}, against {cash_return}.\" Never \"a {pe} of ...\", \"its {correlation}\", "
    "\"high {volatility}\" or \"{expected_return} over the next six months\". Use each key at most once.\n"
    "- No judgement words of your own (strong, weak, high, low, elevated, significant, robust, solid, "
    "minimal, attractive): the keys already say how to read each figure.\n"
    "- Each claim rests on a fact above; no stock phrases such as \"mean reversion potential\" or "
    "\"continued upside potential\".\n"
    "- Do not describe interest-rate moves unless the rate facts show that direction.\n"
    "- Direction and horizon are fixed by the system; do not argue the opposite stance.\n"
    "- The investor holds EUR. Mention currency risk only if the listing or reporting currency is not EUR.\n"
    "- One insider buying is not a cluster. Name a fund's holdings only from the holdings given.\n"
    "- key_risks are risks to the investment, each one short sentence; keys may be used there too. A "
    "missing data feed is not a risk. Write no codes with underscores.\n"
    "- English only.\n"
)


def allowed_digit_terms(*texts: Any) -> list[str]:
    """Words with digits the model may copy as names: the ticker, the fund's name, sector, index."""
    terms: set[str] = set(_FIXED_TERMS)
    for text in texts:
        if not isinstance(text, str):
            continue
        for word in re.findall(r"[A-Za-z0-9&.\-]*\d[A-Za-z0-9&.\-]*", text):
            word = word.strip(".-")
            if word:
                terms.add(word)
                # The ticker without its exchange suffix: IS3S for IS3S.DE.
                terms.update(part for part in word.split(".") if _DIGIT.search(part))
    return sorted(terms, key=len, reverse=True)


def _without_terms(text: str, terms: list[str]) -> str:
    for term in terms:
        text = re.sub(rf"(?<![\w.]){re.escape(term)}(?![\w])", " ", text)
    return text


#: Words a model puts before a key that its phrase already carries: articles,
#: possessives and judgement words ("its {correlation}", "high {volatility}").
_LEAD = frozenset({
    "a", "an", "the", "its", "their", "his", "her", "low", "lower", "high", "higher", "strong", "weak",
    "significant", "elevated", "moderate", "positive", "negative", "robust", "solid", "attractive", "notable",
    "substantial", "considerable", "current", "trailing", "premium", "healthy", "minimal", "limited",
    "annual", "recent", "past", "estimated", "projected", "expected", "stable", "rising", "falling", "large", "small", "sharp", "deep", "big",
})
#: Words that build the sentence around a key; never taken for a repeat of the phrase.
_STRUCTURE = frozenset({
    "with", "at", "by", "via", "from", "for", "on", "in", "to", "and", "as", "despite", "while", "given",
    "has", "have", "shows", "offers", "is", "are", "of", "but", "yet", "though", "against", "over",
})
#: Words a model adds after a key that the phrase already says ("{expected_return} over 6 months").
_TRAILING_FILLER = frozenset({
    "the", "a", "an", "over", "next", "months", "month", "year", "years", "annually", "annual", "six", "twelve",
    "to", "its", "at", "levels", "level", "ratio", "multiple", "estimate", "figure", "measured", "of", "this",
    "period", "horizon", "same", "in", "with", "against", "for", "investor", "alternative", "alternatives",
    "relative", "compared", "benchmark", "portfolio", "portfolios",
})
_TRAILING_STOP = re.compile(
    r"[,;:!?()]|\.(?=\s|$)|\b(?:while|and|but|yet|though|despite|with|which|that|as|offering|suggesting|supported|"
    r"driven|given|outpacing|indicating|reflecting|showing|making|where|so)\b",
    re.I,
)
_WORD_END = re.compile(r"([A-Za-z]+)(\W*)$")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _fill(before: str, phrase: str) -> str:
    """``before`` without the lead-in the phrase already carries, then the phrase.

    From the end of ``before``, articles, judgement words and words of the phrase
    itself are dropped ("its {correlation}", "high {volatility}", "net insider
    selling of {insider_flow}", "with a correlation of {correlation}"); the
    words that build the sentence ("with", "at", "offers") stay unless they are
    part of a run of the phrase's own words.
    """
    if phrase.lower() in before.lower():
        # "{expected_return} over {horizon}": the phrase is already in the sentence.
        return before
    own = set(_words(phrase))
    kept = before
    while True:
        m = _WORD_END.search(kept)
        if not m:
            break
        word = m.group(1).lower()
        if word in _LEAD or (word in own and word not in _STRUCTURE):
            kept = kept[: m.start()]
            continue
        if word in own and word in _STRUCTURE:
            # "correlation of {correlation}": drop "of" only with the phrase word before it.
            prev = _WORD_END.search(kept[: m.start()])
            if prev and prev.group(1).lower() in own and prev.group(1).lower() not in _STRUCTURE:
                kept = kept[: prev.start()]
                continue
        break
    if kept and not kept[-1].isspace() and kept[-1] not in "([":
        kept += " "
    if not kept.strip():
        phrase = phrase[:1].upper() + phrase[1:]
    return kept + phrase


def _trailing(after: str, phrase: str) -> str:
    """``after`` without a fragment that only repeats the phrase ("over 6 months", "to the portfolio")."""
    own = set(_words(phrase))
    # The whole run up to the next punctuation first: "{correlation} with the portfolio."
    punct = re.search(r"[,;:!?()]|\.(?=\s|$)", after)
    run = after[: punct.start()] if punct else after
    run_words = _words(run)
    if run_words and all(w in own or w in _TRAILING_FILLER for w in run_words) and not re.search(r"\d", run):
        return after[len(run):]
    m = _TRAILING_STOP.search(after)
    segment, rest = (after[: m.start()], after[m.start():]) if m else (after, "")
    words = _words(segment)
    if words and all(w in own or w in _TRAILING_FILLER for w in words) and not re.search(r"\d", segment):
        return (" " if rest and rest[0].isalpha() else "") + rest
    return after


def render(text: str, facts: list[Fact], allowed_terms: list[str]) -> tuple[str, list[str]]:
    """Substitute ``{key}`` with its phrase; drop sentences that cite no such key or write a figure.

    Returns the rendered text and one problem per dropped sentence (for the retry
    message and the log).
    """
    by_key = {f.key: f.text for f in facts}
    kept: list[str] = []
    problems: list[str] = []
    for sentence in _SENTENCE.split((text or "").strip()):
        if not sentence:
            continue
        keys = SLOT.findall(sentence)
        unknown = [k for k in keys if k not in by_key]
        if unknown:
            problems.append(f"cites keys that are not in the facts: {', '.join('{' + k + '}' for k in unknown)}")
            continue
        if _DIGIT.search(_without_terms(SLOT.sub(" ", sentence), allowed_terms)):
            problems.append(f"writes a figure itself instead of citing its key: \"{sentence[:120]}\"")
            continue
        out, pos, last_phrase = "", 0, None
        for m in SLOT.finditer(sentence):
            between = sentence[pos : m.start()]
            if last_phrase is not None:
                between = _trailing(between, last_phrase)
            last_phrase = by_key[m.group(1)]
            out = _fill(out + between, last_phrase)
            pos = m.end()
        tail = sentence[pos:]
        if last_phrase is not None:
            tail = _trailing(tail, last_phrase)
        kept.append(re.sub(r"\s+([,.;:!?])", r"\1", out + tail).strip())
    return " ".join(kept).strip(), problems
