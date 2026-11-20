"""Stocks and funds rank apart, one name per industry (audit of the 2026-11-05 Discover run)."""
from __future__ import annotations

from app.decision.discover.pipeline import apply_sector_cap, assign_lane_ranks, select_shortlist


def _result(symbol, score, lane="stock", sector=None, industry=None):
    return {
        "symbol": symbol, "composite_score": score, "lane": lane,
        "scores": {"sentiment_fundamentals": {"sector": sector, "industry": industry}},
    }


def test_funds_compete_only_with_funds():
    ranked = [_result(f"F{i}", 0.9 - i / 100, "fund") for i in range(8)]
    ranked += [_result(f"S{i}", 0.5 - i / 100, sector=f"s{i}", industry=f"i{i}") for i in range(20)]

    selected, _capped = select_shortlist(ranked, 15)

    funds = [r for r in selected if r["lane"] == "fund"]
    assert [r["symbol"] for r in funds] == ["F0", "F1", "F2", "F3", "F4"]
    assert len(selected) == 15
    assert ranked[5]["fund_slots_full"] is True
    assert [r["composite_score"] for r in selected] == sorted((r["composite_score"] for r in selected), reverse=True)


def test_slots_stocks_cannot_fill_go_to_funds():
    ranked = [_result(f"F{i}", 0.5, "fund") for i in range(8)] + [_result("S0", 0.6, sector="s", industry="i")]
    selected, _ = select_shortlist(ranked, 6)
    assert len(selected) == 6
    assert {r["symbol"] for r in selected} >= {"S0", "F0", "F4"}


def test_one_name_per_industry():
    ranked = [
        _result("PM", 0.61, sector="Consumer Defensive", industry="Tobacco"),
        _result("MO", 0.57, sector="Consumer Defensive", industry="Tobacco"),
        _result("KO", 0.55, sector="Consumer Defensive", industry="Beverages"),
    ]
    selected, capped = apply_sector_cap(ranked, 15)
    assert [r["symbol"] for r in selected] == ["PM", "KO"]
    assert [r["symbol"] for r in capped] == ["MO"]


def test_lane_rank_is_within_the_lane():
    ranked = [_result("F", 0.9, "fund"), _result("A", 0.6), _result("B", 0.5)]
    assign_lane_ranks(ranked)
    assert ranked[0]["scores"]["lane_rank"] == 0.5
    assert ranked[1]["scores"]["lane_rank"] == 0.75
    assert ranked[2]["scores"]["lane"] == "stock"
