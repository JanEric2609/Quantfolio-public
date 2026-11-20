"""WP9: the report's numbers, periods and judgement words come from code; planted errors are caught."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import research
from app.foundation import research_facts as rf
from app.foundation.core.db import Base


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _context(news=True, basis="quarter_yoy", months=6, fcf=0.23):
    return {
        "ticker": "WKL.AS", "company": {"name": "Wolters Kluwer"}, "regime": "bull", "risk_profile": "moderate",
        "technicals": {"sessions": 250, "last_close": 100.0, "last_close_date": "2026-11-11", "return_12m_pct": -8.4,
                       "return_3m_pct": 2.1, "rsi_14": 55.0, "volatility_3m_annual_pct": 22.0, "max_drawdown_12m_pct": -35.0,
                       "sma_50": 98.0, "above_sma_50": True, "sma_200": 105.0, "above_sma_200": False},
        "fundamentals": {"revenue_growth": 0.052, "growth_basis": basis, "period_months": months, "fcf_yield": fcf,
                         "pe_ratio": 24.0, "debt_equity": 120, "total_debt": 5e9, "ebitda": 1e9, "cash": 3e8},
        "recent_news": [{"title": "Wolters Kluwer launches product", "source": "x", "published_at": "2026-11-10",
                         "sentiment_label": None}] if news else [],
        "recommendations": [],
    }


FACTS = rf.build_fact_table(_context())


def test_facts_carry_period_unit_source_and_a_label_from_code():
    k = {f.key: f for f in FACTS}
    assert k["revenue_growth"].period == "in the latest half-year against the same half-year a year earlier"
    assert k["volatility"].label == "moderate" and k["drawdown"].label == "deep"
    assert k["fcf_yield"].label.startswith("provider estimate")
    assert "pe" in k and k["pe"].label is None  # no peer set: no judgement word


def test_code_text_never_calls_the_proxy_figure_exceptional():
    text = rf.fundamentals_text(FACTS)
    assert "exceptional" not in text and "provider's free-cash-flow yield" in text and "half-year" in text


def test_the_model_never_sees_a_figure_or_the_proxy():
    seen = rf.qualitative_input(_context(), FACTS)
    assert "23" not in seen and "fcf" not in seen.lower()


# Golden set: one clean text and variants with one planted error each.
CLEAN = "A large publisher whose shares trade below the long-term average, with deep falls in the past year."
VARIANTS = [
    ("clean", CLEAN, True, True),
    ("number off by ten percent", "Revenue grew 5.7 % in the period.", True, False),
    ("number in table", "The volatility is 22.0 % annualised.", True, True),
    ("wrong period", "Revenue grew in the latest quarter.", True, False),
    ("right period", "Revenue grew in the latest half-year.", True, True),
    ("adjective without label", "Free cash flow is exceptional.", True, False),
    ("sentiment without news", "Investors are bullish after the launch.", False, False),
    ("sentiment with news", "Investors are bullish after the launch.", True, True),
    ("unit swap", "Net debt is 22 times earnings.", True, False),
]


@pytest.mark.parametrize("name,text,has_news,ok", VARIANTS)
def test_check_prose_golden_set(name, text, has_news, ok):
    assert (rf.check_prose("Conclusion", text, FACTS, has_news=has_news) == []) is ok, name


def _llm(texts):
    calls = []

    async def call(db, task, messages, **kw):
        calls.append(messages)
        return SimpleNamespace(content=texts[min(len(calls) - 1, len(texts) - 1)])

    return call, calls


def test_a_rejected_section_is_asked_once_more_then_the_template_stands_in():
    call, calls = _llm(["Free cash flow of 99.9 % is exceptional."])
    report = asyncio.run(research._sectioned_report(_memory_db(), _context(), "u", call))
    assert "exceptional" not in report and "99.9" not in report
    assert len(calls) == 8  # four model sections, two tries each
    assert "## Fundamental View" in report and "## Price Action" in report


def test_no_news_means_no_sentiment_call_and_a_plain_sentence():
    call, calls = _llm([CLEAN])
    report = asyncio.run(research._sectioned_report(_memory_db(), _context(news=False), "u", call))
    assert len(calls) == 3  # summary, risks, conclusion
    assert "sentiment cannot be judged" in report


def test_a_clean_answer_is_kept_and_the_model_being_down_is_reported():
    call, _ = _llm([CLEAN])
    report = asyncio.run(research._sectioned_report(_memory_db(), _context(), "u", call))
    assert CLEAN in report

    async def down(*a, **k):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        asyncio.run(research._sectioned_report(_memory_db(), _context(), "u", down))


def test_the_section_name_the_model_repeats_is_dropped():
    from app.foundation.research import _strip_section_name

    assert _strip_section_name("Risk Factors", "Risk Factors. The company faces debt.") == "The company faces debt."
    assert _strip_section_name("Conclusion", "**Conclusion:** Mixed labels.") == "Mixed labels."
    assert _strip_section_name("Conclusion", "## Conclusion\nMixed labels.") == "Mixed labels."
    # Only at the start, and only the section's own name.
    assert _strip_section_name("Conclusion", "In conclusion, mixed.") == "In conclusion, mixed."
    assert _strip_section_name("Sentiment", "Risk Factors. Debt.") == "Risk Factors. Debt."
