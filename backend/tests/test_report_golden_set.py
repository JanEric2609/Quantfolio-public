"""WP9 golden set: 40 seeded fact tables, each with planted-error variants, scored by the production prose check."""
import json
from pathlib import Path

import app.foundation.research_facts as rf

FIXTURE = Path(__file__).parent / "fixtures" / "report_golden_set.json"
GOLDEN = json.loads(FIXTURE.read_text())


def test_the_set_has_enough_tables_and_every_error_class():
    assert len(GOLDEN["items"]) >= 30
    counts: dict[str, int] = {}
    for item in GOLDEN["items"]:
        for v in item["variants"]:
            counts[v["error"]] = counts.get(v["error"], 0) + 1
    for kind in GOLDEN["error_classes"]:
        assert counts.get(kind, 0) >= 10, kind
    assert counts["clean"] == len(GOLDEN["items"])


def test_the_fact_tables_still_match_the_code():
    # Regenerate with `python scripts/report_golden_set.py` when a fact's wording changes on purpose.
    for item in GOLDEN["items"]:
        facts = rf.build_fact_table(item["context"])
        assert [(f.key, f.text, f.unit, f.period, f.label) for f in facts] == \
            [(f["key"], f["text"], f["unit"], f["period"], f["label"]) for f in item["facts"]], item["ticker"]


def test_every_planted_error_is_caught_and_every_clean_text_kept():
    wrong = []
    for item in GOLDEN["items"]:
        facts = rf.build_fact_table(item["context"])
        for v in item["variants"]:
            problems = rf.check_prose("Risk Factors", v["text"], facts, has_news=v["has_news"])
            if (not problems) != v["ok"]:
                wrong.append((item["ticker"], v["error"], v["text"], problems))
    assert wrong == []


def test_a_number_beside_a_named_measure_must_be_that_measure():
    ctx = {"ticker": "X", "technicals": {"volatility_3m_annual_pct": 39.1, "max_drawdown_12m_pct": -43.1}}
    facts = rf.build_fact_table(ctx)
    assert rf.check_prose("Risk Factors", "Volatility was 43.1 % annualised.", facts, has_news=True)
    assert not rf.check_prose("Risk Factors", "The largest fall from a peak was 43.1 %.", facts, has_news=True)
    assert not rf.check_prose("Risk Factors", "It moved 39.1 % a year.", facts, has_news=True)  # no measure named


def test_the_withheld_proxy_figure_may_not_be_repeated():
    ctx = {"ticker": "X", "fundamentals": {"fcf_yield": 0.062}}
    facts = rf.build_fact_table(ctx)
    assert any("withheld" in p for p in rf.check_prose("Risk Factors", "Free cash flow yields 6.2 %.", facts, has_news=True))
    assert "6.2" not in rf.qualitative_input(ctx, facts)
