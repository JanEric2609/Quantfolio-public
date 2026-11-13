"""The stock report's figures, periods and judgement words, written by code (audit 2026-11-12, WP9).

Why. The prompt told the model the free-cash-flow figure was a provider proxy; it
wrote "exceptional 23.0 %" anyway, and it read a half-year as a quarter. A caveat
in a prompt does not stop a small model from over-reading a number. So:

* every sentence that carries a number, a unit, a period or a judgement word is
  written here, from a fact table (value, unit, period label, source, and a
  judgement label computed from a fixed threshold);
* the model writes connecting prose for four sections only, one call each, and
  sees no figure: its input is the judgement labels in words and the headlines;
* :func:`check_prose` runs after generation: no digit that is not in the table,
  no period word the facts do not carry, no unlabelled judgement adjective, no
  sentiment word without news;
* on a failed check the section is asked for once more, then the plain template
  sentence stands in (:func:`template_prose`).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.foundation.fundamentals_context import (
    NET_DEBT_EBITDA_OK,
    book_ratios_unreliable_in,
    growth_basis,
    growth_basis_text,
    net_debt_to_ebitda,
)

#: Sections the model writes; every other section is code.
MODEL_SECTIONS = ("Executive Summary", "Sentiment", "Risk Factors", "Conclusion")

# Fixed thresholds behind the judgement labels (no sector-peer percentile is stored).
RSI_OVERBOUGHT, RSI_OVERSOLD = 70.0, 30.0
VOL_LOW, VOL_HIGH = 15.0, 30.0
DRAWDOWN_DEEP = 30.0


@dataclass(frozen=True)
class Fact:
    key: str
    value: float | None
    text: str  # how code writes the value, e.g. "+4.2 %"
    unit: str  # "%", "x", "price", "count", ""
    period: str  # "last close", "12 months", "latest half-year against a year earlier" ...
    source: str  # "daily closes" | "provider"
    label: str | None = None  # judgement word, computed here


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _bin(value: float, low: float, high: float, words: tuple[str, str, str]) -> str:
    return words[0] if value < low else words[2] if value > high else words[1]


def build_fact_table(context: dict[str, Any]) -> list[Fact]:
    """Facts from the context; a fact with no value is not in the table at all."""
    facts: list[Fact] = []
    t = context.get("technicals") or {}

    def add(key: str, value: Any, text: str, unit: str, period: str, source: str, label: str | None = None) -> None:
        v = _num(value)
        if v is not None:
            facts.append(Fact(key, v, text, unit, period, source, label))

    for key, name, period in (("return_1m_pct", "return_1m", "1 month"), ("return_3m_pct", "return_3m", "3 months"),
                              ("return_6m_pct", "return_6m", "6 months"), ("return_12m_pct", "return_12m", "12 months")):
        v = _num(t.get(key))
        if v is not None:
            add(name, v, f"{v:+.1f} %", "%", period, "daily closes", "gain" if v > 0 else "loss" if v < 0 else None)
    if _num(t.get("rsi_14")) is not None:
        r = float(t["rsi_14"])
        label = "overbought" if r > RSI_OVERBOUGHT else "oversold" if r < RSI_OVERSOLD else "neutral"
        add("rsi_14", r, f"{r:.0f}", "", "14 days", "daily closes", label)
    if _num(t.get("volatility_3m_annual_pct")) is not None:
        v = float(t["volatility_3m_annual_pct"])
        add("volatility", v, f"{v:.1f} %", "%", "last 3 months, annualised", "daily closes",
            _bin(v, VOL_LOW, VOL_HIGH, ("low", "moderate", "high")))
    if _num(t.get("max_drawdown_12m_pct")) is not None:
        v = abs(float(t["max_drawdown_12m_pct"]))
        add("drawdown", v, f"{v:.1f} %", "%", "12 months", "daily closes", "deep" if v >= DRAWDOWN_DEEP else "moderate")
    for key, name in (("sma_50", "50-day"), ("sma_200", "200-day")):
        if t.get(key) is not None and t.get("above_" + key) is not None:
            facts.append(Fact(f"trend_{key}", _num(t[key]), f"{'above' if t['above_' + key] else 'below'} its {name} average",
                              "", name, "daily closes", "above" if t["above_" + key] else "below"))

    f = context.get("fundamentals") or {}
    if f:
        basis = growth_basis_text(growth_basis(f), f.get("period_months"))
        for key, name in (("revenue_growth", "revenue_growth"), ("earnings_growth", "earnings_growth")):
            v = _num(f.get(key))
            if v is not None:
                pct = v * 100 if abs(v) <= 5 else v
                add(name, pct, f"{pct:+.1f} %", "%", basis, "provider",
                    "growing" if pct > 0 else "shrinking" if pct < 0 else None)
        nd = net_debt_to_ebitda(f)
        if nd is not None:
            add("net_debt_ebitda", nd, f"{nd:.1f}x", "x", "latest, provider debt incl. leases over unadjusted EBITDA",
                "provider", "manageable" if nd <= NET_DEBT_EBITDA_OK else "high")
        fcf = _num(f.get("fcf_yield"))
        if fcf is not None:
            pct = fcf * 100 if abs(fcf) <= 1 else fcf
            add("fcf_yield", pct, f"{pct:.1f} %", "%", "provider cash flow over market cap", "provider",
                "provider estimate, not the company's own free cash flow")
        for key, name in (("pe_ratio", "pe"), ("forward_pe", "forward_pe"), ("ev_ebitda", "ev_ebitda"), ("pb_ratio", "pb")):
            v = _num(f.get(key))
            if v is not None:
                add(name, v, f"{v:.1f}", "", "trailing" if key == "pe_ratio" else "provider", "provider")  # no label: no peer set
        if not book_ratios_unreliable_in(f):
            for key, name in (("roe", "roe"),):
                v = _num(f.get(key))
                if v is not None:
                    pct = v * 100 if abs(v) <= 5 else v
                    add(name, pct, f"{pct:.1f} %", "%", "latest", "provider")
    return facts


def _by_key(facts: list[Fact]) -> dict[str, Fact]:
    return {x.key: x for x in facts}


# --- Sections written by code ---------------------------------------------------


def technical_text(facts: list[Fact]) -> str | None:
    k = _by_key(facts)
    parts: list[str] = []
    trends = [k[x].text for x in ("trend_sma_50", "trend_sma_200") if x in k]
    if trends:
        parts.append("The last close is " + " and ".join(trends) + ".")
    if "rsi_14" in k:
        parts.append(f"The 14-day RSI is {k['rsi_14'].text} ({k['rsi_14'].label}).")
    if "volatility" in k:
        parts.append(f"Volatility over the last 3 months is {k['volatility'].text} annualised ({k['volatility'].label}).")
    if "drawdown" in k:
        parts.append(f"The largest fall from a peak in 12 months was {k['drawdown'].text} ({k['drawdown'].label}).")
    return " ".join(parts) or None


def fundamentals_text(facts: list[Fact]) -> str | None:
    k = _by_key(facts)
    parts: list[str] = []
    for key, name in (("revenue_growth", "Revenue"), ("earnings_growth", "Earnings")):
        if key in k:
            parts.append(f"{name} are {k[key].label} ({k[key].text}) {k[key].period}.")
    if "net_debt_ebitda" in k:
        x = k["net_debt_ebitda"]
        parts.append(f"Net debt is {x.text} EBITDA ({x.label}; {x.period}).")
    if "fcf_yield" in k:
        x = k["fcf_yield"]
        parts.append(f"The provider's free-cash-flow yield is {x.text} ({x.label}).")
    for key, name in (("pe", "trailing P/E"), ("forward_pe", "forward P/E"), ("ev_ebitda", "EV/EBITDA"), ("pb", "price/book")):
        if key in k:
            parts.append(f"The {name} is {k[key].text} (no sector comparison is stored, so it is not judged).")
    return " ".join(parts) or None


# --- What the model sees and what it may say --------------------------------------


def qualitative_input(context: dict[str, Any], facts: list[Fact]) -> str:
    """Judgement labels in words and headlines: no figure the model could repeat or misread."""
    lines = [f"Company: {(context.get('company') or {}).get('name') or context['ticker']} ({context['ticker']})"]
    for x in facts:
        if x.label and x.key != "fcf_yield":
            lines.append(f"- {x.key.replace('_', ' ')} ({x.period}): {x.label}")
    news = context.get("recent_news") or []
    if news:
        lines.append("Headlines:")
        lines += [f"- [{n.get('sentiment_label') or 'not scored'}] {n['title']}" for n in news[:5]]
    else:
        lines.append("News: no news source covers this company, so sentiment cannot be judged.")
    lines.append(f"Broad-market regime (not about this stock): {context.get('regime', 'unknown')}")
    lines.append(f"Investor risk profile: {context.get('risk_profile', 'moderate')}")
    return "\n".join(lines)


SECTION_TASKS = {
    "Executive Summary": "Two sentences that say what kind of stock this is from the labels above.",
    "Sentiment": "One or two sentences on what the headlines say about this company. Only the headlines above count.",
    "Risk Factors": "Two or three risks that follow from the labels above (leverage, drawdown, volatility, growth).",
    "Conclusion": "Two sentences weighing the labels against each other. Give no BUY, HOLD or SELL: the verdict is shown elsewhere.",
}

SECTION_SYSTEM = (
    "You write one short section of a stock report in plain English.\n"
    "Rules: type no digits at all, not even in words like '2024'; every figure is written elsewhere. "
    "Use only the labels and headlines given; name no other company, cause, date or size. "
    "Do not use judgement words such as strong, weak, cheap, expensive, exceptional, high or low unless that exact word is "
    "a label above. No heading. Output the section text only."
)

_DIGIT = re.compile(r"\d")
_PERIOD_WORDS = {
    "quarter": ("quarter", "quarterly"),
    "half-year": ("half-year", "half year", "semi-annual"),
    "ttm": ("trailing twelve", "trailing 12", "ttm"),
}
SENTIMENT_WORDS = ("bullish", "bearish", "optimis", "pessimis", "investor sentiment", "market sentiment",
                   "positive sentiment", "negative sentiment")
JUDGEMENT_WORDS = ("exceptional", "outstanding", "strong", "weak", "cheap", "expensive", "undervalued", "overvalued",
                   "excellent", "poor", "high", "low", "attractive")


def check_prose(section: str, text: str, facts: list[Fact], *, has_news: bool) -> list[str]:
    """Problems with the model's text for one section; empty when it may be used."""
    problems: list[str] = []
    low = text.lower()
    if _DIGIT.search(text):
        # A digit is tolerated only where code wrote the same number within 1 %.
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(%|percent|x\b|times)?", text):
            v = float(m.group(1).replace(",", "."))
            tail = (m.group(2) or "").lower()
            unit = "%" if tail in ("%", "percent") else "x" if tail in ("x", "times") else None
            if not any(
                x.value is not None and abs(v - abs(x.value)) <= 0.01 * max(abs(x.value), 1e-9)
                and (unit is None or x.unit == unit)
                for x in facts
            ):
                problems.append(f"number {m.group(0).strip()} is not in the fact table with that unit")
    allowed_periods = " ".join(x.period.lower() for x in facts)
    for name, words in _PERIOD_WORDS.items():
        if any(w in low for w in words) and not any(w in allowed_periods for w in words):
            problems.append(f"period word '{name}' does not match the facts")
    if not has_news and any(w in low for w in SENTIMENT_WORDS):
        problems.append("sentiment claim without news")
    labels = " ".join((x.label or "").lower() for x in facts)
    for w in JUDGEMENT_WORDS:
        if re.search(rf"\b{w}\b", low) and not re.search(rf"\b{w}\b", labels):
            problems.append(f"judgement word '{w}' has no label")
    return problems


def template_prose(section: str, context: dict[str, Any], facts: list[Fact]) -> str | None:
    """The plain sentence that stands in for a section the model could not write; None drops the section."""
    name = (context.get("company") or {}).get("name") or context["ticker"]
    labelled = [x for x in facts if x.label and x.key != "fcf_yield"]
    if section == "Sentiment":
        if not (context.get("recent_news") or []):
            return "No news source covers this company, so sentiment cannot be judged from the data."
        return f"{len(context['recent_news'][:5])} recent headlines are listed with the report; they are not summarised here."
    if not labelled:
        return None
    if section == "Executive Summary":
        return f"{name}: the figures in the sections below are written from the stored data."
    if section == "Risk Factors":
        risks = []
        k = _by_key(facts)
        if k.get("net_debt_ebitda") and k["net_debt_ebitda"].label == "high":
            risks.append("leverage is high on the provider's definition")
        if k.get("drawdown") and k["drawdown"].label == "deep":
            risks.append("the stock fell deeply from a peak within the year")
        if k.get("volatility") and k["volatility"].label == "high":
            risks.append("price swings are large")
        return ("Risks from the data: " + "; ".join(risks) + ".") if risks else "The stored data shows no risk beyond ordinary market risk."
    if section == "Conclusion":
        return "The labels above are the full basis of this report; the verdict is shown separately."
    return None
