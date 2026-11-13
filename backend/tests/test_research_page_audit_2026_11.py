from app.foundation.providers.yfinance_provider import _epoch_date
from app.foundation.research import _build_user_message, normalise_verdicts


def test_normalise_verdicts_drops_invalid_and_tags_uncalibrated():
    raw = [
        {"horizon": "1m", "verdict": "buy", "confidence": 1.7},
        {"horizon": "1M", "verdict": "SELL", "confidence": 0.2},  # duplicate: first wins
        {"horizon": "unknown", "verdict": "HOLD", "confidence": 0.5},
        {"horizon": "5y+", "verdict": "MAYBE", "confidence": 0.5},
        {"horizon": "1-3y", "verdict": "HOLD", "confidence": "high"},
        {"horizon": "6m-1y", "verdict": "HOLD", "confidence": 0.61},
        "junk",
    ]
    out = normalise_verdicts(raw)
    assert [v["horizon"] for v in out] == ["1m", "6m-1y"]  # 1-3y and 5y+ are beyond one year
    assert out[0] == {"horizon": "1m", "verdict": "BUY", "confidence": None, "source": "llm_uncalibrated"}


def test_normalise_verdicts_non_list_is_empty():
    assert normalise_verdicts({"horizon": "1m"}) == []
    assert normalise_verdicts([]) == []


def test_epoch_date():
    assert _epoch_date(1782777600) == "2026-06-30"
    assert _epoch_date(None) is None
    assert _epoch_date("x") is None


def test_prompt_names_period_end_and_fcf_basis():
    ctx = {
        "ticker": "WKL.AS", "quote": {}, "risk_profile": "moderate", "regime": "sideways",
        "fundamentals": {"revenue_growth": -0.006, "earnings_growth": 0.089, "fcf_yield": 0.064,
                         "growth_basis": "quarter_yoy", "latest_period_end": "2026-06-30"},
    }
    msg = _build_user_message(ctx)
    assert "2026-06-30" in msg
    assert "before lease payments" in msg
