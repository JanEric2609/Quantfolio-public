"""The report golden set (plan 2026-11-12, WP9): seeded fact tables, each with planted-error variants.

Each item is one report context, its fact table (computed by ``research_facts``), the messages the model gets per
section (the input of the LLM A/B, WP15.3) and a list of variants: texts a model might write, each with one planted
error of a known class or none, and whether ``check_prose`` must accept it. Scored by code; no network, no database.

    python scripts/report_golden_set.py --out tests/fixtures/report_golden_set.json   # regenerate the fixture

The fixture is committed; ``tests/test_report_golden_set.py`` fails when the code's fact tables drift from it or
when the checker lets a planted error through.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

N_TABLES = 40
SEED = 20261112
#: Error classes of the plan (WP9 "Golden set"); each table carries every class its facts allow.
ERROR_CLASSES = ("wrong_period", "proxy_figure", "adjective_without_label", "sentiment_without_news",
                 "number_off_by_ten_percent", "unit_swap")


def _context(i: int, rng: random.Random) -> dict[str, Any]:
    has_news = rng.random() >= 0.3
    return {
        "ticker": f"T{i:02d}.AS", "company": {"name": f"Company {chr(65 + i % 26)}{i}"},
        "regime": rng.choice(["bull", "bear", "neutral"]), "risk_profile": "moderate",
        "technicals": {
            "sessions": 250, "last_close": round(rng.uniform(20, 300), 2), "last_close_date": "2026-11-11",
            "return_12m_pct": round(rng.uniform(-40, 60), 1), "return_3m_pct": round(rng.uniform(-15, 20), 1),
            "rsi_14": round(rng.uniform(25, 75), 1), "volatility_3m_annual_pct": round(rng.uniform(10, 55), 1),
            "max_drawdown_12m_pct": round(-rng.uniform(5, 50), 1), "sma_50": 100.0, "above_sma_50": rng.random() < 0.5,
            "sma_200": 100.0, "above_sma_200": rng.random() < 0.5,
        },
        "fundamentals": {
            "revenue_growth": round(rng.uniform(-0.1, 0.3), 3), "growth_basis": "quarter_yoy",
            "period_months": rng.choice([3, 6, 12]),
            # A third of the tables carry the provider's FCF proxy (the model is never given it).
            "fcf_yield": round(rng.uniform(0.01, 0.12), 3) if rng.random() < 0.67 else None,
            "pe_ratio": round(rng.uniform(6, 45), 1), "debt_equity": round(rng.uniform(10, 250)),
            "total_debt": 5e9, "ebitda": round(rng.uniform(0.8, 3.0), 2) * 1e9, "cash": 3e8,
        },
        "recent_news": [{"title": f"Company {i} announces an update", "source": "x", "published_at": "2026-11-10",
                         "sentiment_label": None}] if has_news else [],
        "recommendations": [],
    }


def _variants(facts: list[Any], has_news: bool) -> list[dict[str, Any]]:
    """Clean controls and one text per error class the table allows."""
    from app.foundation.research_facts import JUDGEMENT_WORDS

    by = {f.key: f for f in facts}
    labels = " ".join((f.label or "").lower() for f in facts)
    periods = " ".join(f.period.lower() for f in facts)
    out: list[dict[str, Any]] = []

    def add(kind: str, text: str, ok: bool, *, news: bool = has_news) -> None:
        out.append({"error": kind, "text": text, "has_news": news, "ok": ok})

    trend = by.get("trend_sma_200")
    dd = by.get("drawdown")
    # No digits: the model is told to type none ("200-day" would be one).
    clean = "The shares sit " + (f"{trend.label} their long-term average" if trend else "near their averages")
    clean += f", and the largest fall from a peak in the past year was {dd.label}." if dd else "."
    add("clean", clean, True)
    vol = by.get("volatility")
    if vol:
        add("clean_number_from_table", f"Volatility was {vol.text} annualised.", True)
        add("number_off_by_ten_percent", f"Volatility was {vol.value * 1.1:.1f} % annualised.", False)
        add("unit_swap", f"Volatility was {vol.value:.1f} times the market's.", False)
    growth = by.get("revenue_growth")
    if growth:
        wrong = next(w for w in ("quarter", "half-year", "trailing twelve") if w not in periods)
        add("wrong_period", f"Revenue grew in the latest {wrong} against a year earlier.", False)
    if "fcf_yield" in by:
        add("proxy_figure", f"Free cash flow yields {by['fcf_yield'].text} of the market value.", False)
    word = next(w for w in JUDGEMENT_WORDS if w not in labels.split())
    add("adjective_without_label", f"The balance sheet looks {word}.", False)
    if has_news:
        add("clean_sentiment_with_news", "Investors are optimistic after the announcement.", True)
    else:
        add("sentiment_without_news", "Investors are optimistic after the announcement.", False)
    return out


def build(n: int = N_TABLES, seed: int = SEED) -> dict[str, Any]:
    import app.foundation.research_facts as rf

    rng = random.Random(seed)
    items = []
    for i in range(n):
        context = _context(i, rng)
        facts = rf.build_fact_table(context)
        has_news = bool(context["recent_news"])
        items.append({
            "ticker": context["ticker"], "context": context, "has_news": has_news,
            "facts": [{"key": f.key, "value": f.value, "text": f.text, "unit": f.unit, "period": f.period,
                       "label": f.label} for f in facts],
            "messages_by_section": {
                sec: [{"role": "system", "content": rf.SECTION_SYSTEM},
                      {"role": "user", "content": f"{rf.qualitative_input(context, facts)}\n\nSection: {sec}. {rf.SECTION_TASKS[sec]}"}]
                for sec in rf.MODEL_SECTIONS if not (sec == "Sentiment" and not has_news)},
            "variants": _variants(facts, has_news),
        })
    return {"seed": seed, "error_classes": list(ERROR_CLASSES), "items": items}


def score(golden: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per error class (and the clean controls): how many variants the checker judged as expected."""
    import app.foundation.research_facts as rf

    out: dict[str, dict[str, int]] = {}
    for item in golden["items"]:
        facts = rf.build_fact_table(item["context"])
        for v in item["variants"]:
            problems = rf.check_prose("Risk Factors", v["text"], facts, has_news=v["has_news"])
            row = out.setdefault(v["error"], {"n": 0, "as_expected": 0})
            row["n"] += 1
            row["as_expected"] += int((not problems) == v["ok"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures/report_golden_set.json"))
    a = ap.parse_args()
    data = build()
    a.out.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {len(data['items'])} fact tables to {a.out}")
    for kind, row in sorted(score(data).items()):
        print(f"{kind:30s} {row['as_expected']}/{row['n']}")
