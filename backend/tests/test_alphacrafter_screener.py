"""Tests for the AlphaCrafter Screener: regime-conditioned factor selection."""

import asyncio
import json
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import FactorsLibrary, ScreenerRun
from app.lab.alphacrafter.screener import (
    ScreenerAgent,
    base_quality,
    infer_category,
    regime_affinity,
    run_screener,
    series_correlation,
)
from app.lab.alphacrafter.shared_memory import (
    FactorMetrics,
    FactorState,
    MarketState,
    SharedMemoryH,
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # regime_snapshots is a raw-SQL hypertable — create it manually.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS regime_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TIMESTAMP NOT NULL,
                    label TEXT NOT NULL,
                    score REAL NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT DEFAULT '{}'
                )
                """
            )
        )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _add_factor(
    db,
    name: str,
    source: str = "seed",
    ic: float = 0.05,
    icir: float = 1.0,
    ic_series: list | None = None,
    formula: str = "",
) -> str:
    """Insert a FactorsLibrary row and return its id."""
    fid = str(uuid.uuid4())
    series = ic_series if ic_series is not None else [ic] * 12
    ic_summary = json.dumps({"ic": ic, "icir": icir, "ic_series": series})
    formula_json = json.dumps({"formula": formula, "dsl": None, "name": name})
    row = FactorsLibrary(
        id=fid,
        name=name,
        formula_json=formula_json,
        source=source,
        ic_summary_json=ic_summary,
    )
    db.add(row)
    db.commit()
    return fid


# ---------------------------------------------------------------------------
# Pure-function unit tests: infer_category
# ---------------------------------------------------------------------------


def test_infer_category_momentum():
    assert infer_category("momentum_12_1") == "momentum"


def test_infer_category_momentum_trend():
    assert infer_category("trend_following") == "momentum"


def test_infer_category_quality():
    assert infer_category("quality_roe") == "quality"


def test_infer_category_quality_roa():
    assert infer_category("factor_roa_trailing") == "quality"


def test_infer_category_low_vol():
    assert infer_category("volatility_21d") == "low_vol"


def test_infer_category_low_vol_via_beta():
    assert infer_category("market_beta_60d") == "low_vol"


def test_infer_category_value_from_name():
    assert infer_category("value_ep") == "value"


def test_infer_category_value_from_formula():
    # Name alone doesn't match value; formula has "pe_ratio".
    assert infer_category("custom_factor", formula="1 / pe_ratio") == "value"


def test_infer_category_size():
    assert infer_category("size_log_mc") == "size"


def test_infer_category_unknown():
    assert infer_category("some_esoteric_factor_xyz") == "unknown"


# ---------------------------------------------------------------------------
# Pure-function unit tests: regime_affinity
# ---------------------------------------------------------------------------


def test_regime_affinity_momentum_bull_gt_bear():
    bull = regime_affinity("momentum", "bull", crisis=False)
    bear = regime_affinity("momentum", "bear", crisis=False)
    assert bull > bear


def test_regime_affinity_low_vol_bear_gt_bull():
    bull = regime_affinity("low_vol", "bull", crisis=False)
    bear = regime_affinity("low_vol", "bear", crisis=False)
    assert bear > bull


def test_regime_affinity_crisis_overrides_bull_for_momentum():
    """Crisis multiplier for momentum (0.4) is lower than bear (0.6)."""
    bear = regime_affinity("momentum", "bear", crisis=False)
    crisis = regime_affinity("momentum", "bull", crisis=True)
    assert crisis < bear


def test_regime_affinity_crisis_favours_low_vol():
    """Crisis multiplier for low_vol (1.5) > bear multiplier (1.3)."""
    bear = regime_affinity("low_vol", "bear", crisis=False)
    crisis = regime_affinity("low_vol", "bear", crisis=True)
    assert crisis > bear


def test_regime_affinity_unknown_regime_returns_neutral():
    assert regime_affinity("quality", "nonexistent_regime", crisis=False) == 1.0


def test_regime_affinity_unknown_category_neutral():
    assert regime_affinity("unknown", "bull", crisis=False) == 1.0


# ---------------------------------------------------------------------------
# Pure-function unit tests: base_quality
# ---------------------------------------------------------------------------


def test_base_quality_increases_with_ic():
    low = base_quality(0.01, 1.0)
    high = base_quality(0.10, 1.0)
    assert high > low


def test_base_quality_increases_with_icir():
    low = base_quality(0.05, 0.5)
    high = base_quality(0.05, 2.0)
    assert high > low


def test_base_quality_uses_absolute_ic():
    pos = base_quality(0.05, 1.0)
    neg = base_quality(-0.05, 1.0)
    assert pos == neg


def test_base_quality_clips_icir_at_3():
    clipped = base_quality(0.05, 3.0)
    over = base_quality(0.05, 10.0)
    assert clipped == over


def test_base_quality_zero_ic():
    assert base_quality(0.0, 5.0) == 0.0


# ---------------------------------------------------------------------------
# Pure-function unit tests: series_correlation
# ---------------------------------------------------------------------------


def test_series_correlation_identical_near_one():
    s = [0.1, 0.2, -0.1, 0.3, 0.05]
    assert series_correlation(s, s) > 0.99


def test_series_correlation_anti_near_minus_one():
    s = [0.1, 0.2, -0.1, 0.3, 0.05]
    neg = [-x for x in s]
    assert series_correlation(s, neg) < -0.99


def test_series_correlation_fewer_than_3_points_returns_zero():
    assert series_correlation([0.1, 0.2], [0.1, 0.2]) == 0.0


def test_series_correlation_single_point_returns_zero():
    assert series_correlation([0.5], [0.5]) == 0.0


def test_series_correlation_constant_series_returns_zero():
    assert series_correlation([1.0, 1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0]) == 0.0


def test_series_correlation_constant_one_series_returns_zero():
    assert series_correlation([0.1, 0.2, 0.3], [1.0, 1.0, 1.0]) == 0.0


# ---------------------------------------------------------------------------
# Integration: bull vs bear selects different factor sets
# ---------------------------------------------------------------------------


def test_bull_favours_momentum_over_low_vol():
    """With equal ic/icir, momentum ranks first in bull regime."""
    db = _memory_db()
    mom_id = _add_factor(db, "momentum_12_1", ic=0.05, icir=1.0)
    low_vol_id = _add_factor(db, "volatility_21d", ic=0.05, icir=1.0)

    result = asyncio.run(
        run_screener(db, [mom_id, low_vol_id], regime_label="bull", crisis=False)
    )
    # Both may be selected; but momentum must rank above low_vol.
    assert result.selected_factor_ids[0] == mom_id, (
        f"Expected momentum first in bull, got {result.selected_factor_ids}"
    )


def test_bear_favours_low_vol_over_momentum():
    """With equal ic/icir, low_vol ranks first in bear regime."""
    db = _memory_db()
    mom_id = _add_factor(db, "momentum_12_1", ic=0.05, icir=1.0)
    low_vol_id = _add_factor(db, "volatility_21d", ic=0.05, icir=1.0)

    result = asyncio.run(
        run_screener(db, [mom_id, low_vol_id], regime_label="bear", crisis=False)
    )
    assert result.selected_factor_ids[0] == low_vol_id, (
        f"Expected low_vol first in bear, got {result.selected_factor_ids}"
    )


def test_sideways_favours_value_over_momentum():
    """Value affinity (1.2) > momentum (0.9) in sideways regime."""
    db = _memory_db()
    mom_id = _add_factor(db, "momentum_12_1", ic=0.05, icir=1.0)
    val_id = _add_factor(db, "value_ep", ic=0.05, icir=1.0)

    result = asyncio.run(
        run_screener(db, [mom_id, val_id], regime_label="sideways", crisis=False)
    )
    assert result.selected_factor_ids[0] == val_id, (
        f"Expected value first in sideways, got {result.selected_factor_ids}"
    )


# ---------------------------------------------------------------------------
# Integration: crisis gate suppresses LLM factors
# ---------------------------------------------------------------------------


def test_crisis_suppresses_llm_factor():
    db = _memory_db()
    seed_id = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    llm_id = _add_factor(db, "llm_alpha_factor", source="llm", ic=0.10, icir=2.0)

    result = asyncio.run(
        run_screener(db, [seed_id, llm_id], regime_label="bull", crisis=True)
    )
    assert llm_id not in result.selected_factor_ids, "LLM factor must be excluded in crisis"
    rejected_ids = [r["factor_id"] for r in result.scores["rejected"]]
    assert llm_id in rejected_ids, "LLM factor must appear in rejected list"


def test_crisis_llm_rejected_reason():
    db = _memory_db()
    llm_id = _add_factor(db, "llm_signal_x", source="llm", ic=0.10, icir=2.0)

    result = asyncio.run(
        run_screener(db, [llm_id], regime_label="bull", crisis=True)
    )
    rejected = {r["factor_id"]: r for r in result.scores["rejected"]}
    assert rejected[llm_id]["rejected_reason"] == "crisis_llm_suppressed"


def test_non_llm_factor_not_suppressed_in_crisis():
    db = _memory_db()
    seed_id = _add_factor(db, "volatility_21d", source="seed", ic=0.05, icir=1.0)

    result = asyncio.run(
        run_screener(db, [seed_id], regime_label="bear", crisis=True)
    )
    assert seed_id in result.selected_factor_ids


# ---------------------------------------------------------------------------
# Integration: correlation deduplication
# ---------------------------------------------------------------------------


def test_correlated_factors_deduplicated():
    """Two factors with near-identical IC series — only one survives selection."""
    db = _memory_db()
    shared_series = [0.05, 0.06, 0.04, 0.07, 0.05, 0.06, 0.04, 0.05, 0.06, 0.05]
    f1_id = _add_factor(db, "momentum_12_1", ic=0.06, icir=1.2, ic_series=shared_series)
    f2_id = _add_factor(
        db, "trend_60d", ic=0.05, icir=1.0,
        ic_series=[x + 0.0001 for x in shared_series],  # virtually identical
    )

    result = asyncio.run(
        run_screener(db, [f1_id, f2_id], regime_label="bull", crisis=False, top_n=5)
    )
    selected = result.selected_factor_ids
    assert not (f1_id in selected and f2_id in selected), (
        "Both correlated factors should not both be selected"
    )

    rejected = result.scores["rejected"]
    corr_rejected = [r for r in rejected if (r.get("rejected_reason") or "").startswith("correlated_with:")]
    assert len(corr_rejected) >= 1, "At least one factor must be rejected as correlated"


def test_uncorrelated_factors_both_selected():
    """Factors with very different IC series are both kept."""
    db = _memory_db()
    s1 = [0.1, -0.1, 0.1, -0.1, 0.1, -0.1, 0.1, -0.1, 0.1, -0.1]
    s2 = [0.05, 0.06, 0.07, 0.06, 0.05, 0.06, 0.07, 0.06, 0.05, 0.06]
    f1_id = _add_factor(db, "momentum_12_1", ic=0.05, icir=1.0, ic_series=s1)
    f2_id = _add_factor(db, "quality_roe", ic=0.05, icir=1.0, ic_series=s2)

    result = asyncio.run(
        run_screener(db, [f1_id, f2_id], regime_label="bull", crisis=False, top_n=5)
    )
    assert f1_id in result.selected_factor_ids
    assert f2_id in result.selected_factor_ids


# ---------------------------------------------------------------------------
# Integration: reads regime label from regime_snapshots table
# ---------------------------------------------------------------------------


def test_reads_regime_from_snapshot():
    """When regime_label/crisis are not passed, screener reads from regime_snapshots."""
    db = _memory_db()
    low_vol_id = _add_factor(db, "volatility_21d", source="seed", ic=0.05, icir=1.0)
    mom_id = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)

    # Seed a regime_snapshots row with label="bear", crisis=False.
    db.execute(
        text(
            "INSERT INTO regime_snapshots (ts, label, score, source, payload_json) "
            "VALUES (:ts, :label, :score, :source, :payload_json)"
        ),
        {
            "ts": "2026-05-31T12:00:00",
            "label": "bear",
            "score": 0.7,
            "source": "hmm",
            "payload_json": json.dumps({"crisis": False}),
        },
    )
    db.commit()

    # Call WITHOUT overrides — should pick up "bear" from snapshot.
    result = asyncio.run(run_screener(db, [low_vol_id, mom_id]))
    assert result.regime_label == "bear"
    # In bear, low_vol affinity (1.3) > momentum (0.6), so low_vol leads.
    assert result.selected_factor_ids[0] == low_vol_id


# ---------------------------------------------------------------------------
# Integration: persists ScreenerRun row
# ---------------------------------------------------------------------------


def test_persists_screener_run_row():
    db = _memory_db()
    fid = _add_factor(db, "quality_roe", ic=0.05, icir=1.0)

    result = asyncio.run(
        run_screener(db, [fid], regime_label="bull", crisis=False)
    )

    runs = db.execute(select(ScreenerRun)).scalars().all()
    assert len(runs) == 1

    run = runs[0]
    assert run.id == result.screener_run_id
    payload = json.loads(run.scores_json)
    assert "selected" in payload
    assert "rejected" in payload
    # selected_factor_ids in the DB row is comma-joined.
    assert run.selected_factor_ids == ",".join(result.selected_factor_ids)


def test_screener_run_selected_ids_match_return():
    """DB row selected_factor_ids matches what run_screener returns."""
    db = _memory_db()
    f1 = _add_factor(db, "momentum_12_1", ic=0.06, icir=1.2)
    f2 = _add_factor(db, "quality_roe", ic=0.04, icir=0.8)

    result = asyncio.run(
        run_screener(db, [f1, f2], regime_label="bull", crisis=False)
    )
    run = db.execute(select(ScreenerRun).where(ScreenerRun.id == result.screener_run_id)).scalar_one()
    stored_ids = run.selected_factor_ids.split(",") if run.selected_factor_ids else []
    assert stored_ids == result.selected_factor_ids


# ---------------------------------------------------------------------------
# Integration: empty candidates
# ---------------------------------------------------------------------------


def test_empty_candidates_returns_empty_selection():
    db = _memory_db()
    result = asyncio.run(run_screener(db, [], regime_label="bull", crisis=False))
    assert result.selected_factor_ids == []


def test_empty_candidates_still_persists_screener_run():
    db = _memory_db()
    asyncio.run(run_screener(db, [], regime_label="bull", crisis=False))
    runs = db.execute(select(ScreenerRun)).scalars().all()
    assert len(runs) == 1


def test_empty_candidates_regime_score_is_zero():
    db = _memory_db()
    result = asyncio.run(run_screener(db, [], regime_label="sideways", crisis=False))
    assert result.regime_score == 0.0


# ---------------------------------------------------------------------------
# Integration: top_n limit
# ---------------------------------------------------------------------------


def test_top_n_limits_selection():
    db = _memory_db()
    # Seed 5 uncorrelated factors, request only 2.
    series = [
        [0.1 * i, -0.05 * i, 0.08 * i, 0.03 * i, 0.12 * i, -0.02 * i, 0.07 * i]
        for i in range(1, 6)
    ]
    ids = []
    names = ["momentum_12_1", "quality_roe", "value_ep", "volatility_21d", "size_log_mc"]
    for name, s in zip(names, series):
        ids.append(_add_factor(db, name, ic=0.05, icir=1.0, ic_series=s))

    result = asyncio.run(
        run_screener(db, ids, regime_label="bull", crisis=False, top_n=2)
    )
    assert len(result.selected_factor_ids) <= 2


# ---------------------------------------------------------------------------
# Helper: create SharedMemoryH for tests
# ---------------------------------------------------------------------------


def _make_h(
    regime_label: str | None = "bull",
    crisis: bool = False,
    factor_states: list | None = None,
) -> SharedMemoryH:
    """Create a SharedMemoryH instance for testing."""
    market_state = MarketState(
        universe=["AAPL", "MSFT", "GOOGL"],
        date_range=("2025-01-01", "2025-12-31"),
        regime_label=regime_label,
        crisis=crisis,
        macro_indicators={},
        data_availability={},
    )
    return SharedMemoryH(
        market_state=market_state,
        factor_states=factor_states or [],
    )


def _make_factor_state(
    factor_id: str,
    name: str,
    source: str = "seed",
    category: str = "momentum",
    ic: float = 0.05,
    icir: float = 1.0,
    turnover: float = 0.1,
    n_obs: int = 252,
) -> FactorState:
    """Create a FactorState instance for testing."""
    return FactorState(
        id=factor_id,
        name=name,
        source=source,
        category=category,
        formula=name,
        dsl=None,
        metrics=FactorMetrics(
            ic=ic,
            icir=icir,
            turnover=turnover,
            decay_halflife_days=None,
            n_obs=n_obs,
        ),
        regime_applicability={},
    )


# ---------------------------------------------------------------------------
# ScreenerAgent tests: reads regime from H (not RegimeStore)
# ---------------------------------------------------------------------------


def test_screener_agent_reads_regime_from_h():
    """ScreenerAgent reads regime_label from H.market_state, not RegimeStore."""
    db = _memory_db()
    fid = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    factor_state = _make_factor_state(fid, "momentum_12_1", source="seed", category="momentum")
    h = _make_h(regime_label="bear", crisis=False, factor_states=[factor_state])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    assert result_h.regime_assessment["regime_label"] == "bear"


def test_screener_agent_reads_crisis_from_h():
    """ScreenerAgent reads crisis from H.market_state."""
    db = _memory_db()
    fid = _add_factor(db, "volatility_21d", source="seed", ic=0.05, icir=1.0)
    factor_state = _make_factor_state(fid, "volatility_21d", source="seed", category="low_vol")
    h = _make_h(regime_label="bear", crisis=True, factor_states=[factor_state])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    assert result_h.regime_assessment["crisis"] is True


# ---------------------------------------------------------------------------
# ScreenerAgent tests: crisis gate suppresses LLM factors
# ---------------------------------------------------------------------------


def test_screener_agent_crisis_suppresses_llm_factors():
    """When H.market_state.crisis is True, LLM factors are suppressed."""
    db = _memory_db()
    seed_id = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    llm_id = _add_factor(db, "llm_alpha_factor", source="llm", ic=0.10, icir=2.0)

    seed_fs = _make_factor_state(seed_id, "momentum_12_1", source="seed", category="momentum")
    llm_fs = _make_factor_state(llm_id, "llm_alpha_factor", source="llm", category="momentum")

    h = _make_h(regime_label="bull", crisis=True, factor_states=[seed_fs, llm_fs])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    selected_ids = result_h.screener_outputs.get("selected_factor_ids", [])
    assert llm_id not in selected_ids, "LLM factor must be excluded in crisis"


def test_screener_agent_crisis_rejects_llm_factor():
    """LLM factor appears in rejected list with correct reason during crisis."""
    db = _memory_db()
    llm_id = _add_factor(db, "llm_signal_x", source="llm", ic=0.10, icir=2.0)
    llm_fs = _make_factor_state(llm_id, "llm_signal_x", source="llm", category="momentum")

    h = _make_h(regime_label="bull", crisis=True, factor_states=[llm_fs])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    rejected = result_h.screener_outputs.get("rejected", [])
    rejected_map = {r["factor_id"]: r for r in rejected}
    assert rejected_map[llm_id]["rejected_reason"] == "crisis_llm_suppressed"


def test_screener_agent_no_crisis_allows_llm_factors():
    """Without crisis, LLM factors are not suppressed."""
    db = _memory_db()
    llm_id = _add_factor(db, "llm_alpha_factor", source="llm", ic=0.10, icir=2.0)
    llm_fs = _make_factor_state(llm_id, "llm_alpha_factor", source="llm", category="momentum")

    h = _make_h(regime_label="bull", crisis=False, factor_states=[llm_fs])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    selected_ids = result_h.screener_outputs.get("selected_factor_ids", [])
    assert llm_id in selected_ids, "LLM factor should be selected when no crisis"


# ---------------------------------------------------------------------------
# ScreenerAgent tests: writes regime_assessment to H
# ---------------------------------------------------------------------------


def test_screener_agent_writes_regime_assessment():
    """ScreenerAgent writes regime_assessment to H."""
    db = _memory_db()
    fid = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    factor_state = _make_factor_state(fid, "momentum_12_1", source="seed", category="momentum")
    h = _make_h(regime_label="bull", crisis=False, factor_states=[factor_state])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    assert "regime_label" in result_h.regime_assessment
    assert "crisis" in result_h.regime_assessment
    assert "selected_factor_ids" in result_h.regime_assessment
    assert "regime_score" in result_h.regime_assessment
    assert "rejected" in result_h.regime_assessment


def test_screener_agent_writes_agent_history():
    """ScreenerAgent appends to H.agent_history."""
    db = _memory_db()
    fid = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    factor_state = _make_factor_state(fid, "momentum_12_1", source="seed", category="momentum")
    h = _make_h(regime_label="bull", crisis=False, factor_states=[factor_state])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    assert len(result_h.agent_history) == 1
    assert result_h.agent_history[0]["agent"] == "screener"
    assert result_h.agent_history[0]["action"] == "run"


def test_screener_agent_writes_screener_outputs():
    """ScreenerAgent writes to H.screener_outputs."""
    db = _memory_db()
    fid = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    factor_state = _make_factor_state(fid, "momentum_12_1", source="seed", category="momentum")
    h = _make_h(regime_label="bull", crisis=False, factor_states=[factor_state])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    assert result_h.screener_outputs.get("selected_factor_ids") == [fid]


# ---------------------------------------------------------------------------
# ScreenerAgent tests: selects factors based on regime from H
# ---------------------------------------------------------------------------


def test_screener_agent_bull_favours_momentum():
    """With equal ic/icir, momentum ranks first in bull regime via H."""
    db = _memory_db()
    mom_id = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    low_vol_id = _add_factor(db, "volatility_21d", source="seed", ic=0.05, icir=1.0)

    mom_fs = _make_factor_state(mom_id, "momentum_12_1", source="seed", category="momentum")
    low_vol_fs = _make_factor_state(low_vol_id, "volatility_21d", source="seed", category="low_vol")

    h = _make_h(regime_label="bull", crisis=False, factor_states=[mom_fs, low_vol_fs])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    selected_ids = result_h.screener_outputs.get("selected_factor_ids", [])
    assert selected_ids[0] == mom_id, (
        f"Expected momentum first in bull, got {selected_ids}"
    )


def test_screener_agent_bear_favours_low_vol():
    """With equal ic/icir, low_vol ranks first in bear regime via H."""
    db = _memory_db()
    mom_id = _add_factor(db, "momentum_12_1", source="seed", ic=0.05, icir=1.0)
    low_vol_id = _add_factor(db, "volatility_21d", source="seed", ic=0.05, icir=1.0)

    mom_fs = _make_factor_state(mom_id, "momentum_12_1", source="seed", category="momentum")
    low_vol_fs = _make_factor_state(low_vol_id, "volatility_21d", source="seed", category="low_vol")

    h = _make_h(regime_label="bear", crisis=False, factor_states=[mom_fs, low_vol_fs])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    selected_ids = result_h.screener_outputs.get("selected_factor_ids", [])
    assert selected_ids[0] == low_vol_id, (
        f"Expected low_vol first in bear, got {selected_ids}"
    )


# ---------------------------------------------------------------------------
# ScreenerAgent tests: empty factor_states
# ---------------------------------------------------------------------------


def test_screener_agent_empty_factor_states():
    """ScreenerAgent handles empty factor_states gracefully."""
    db = _memory_db()
    h = _make_h(regime_label="bull", crisis=False, factor_states=[])

    agent = ScreenerAgent()
    result_h = agent.run(h, db)

    assert result_h.screener_outputs.get("selected_factor_ids") == []
    assert result_h.regime_assessment.get("regime_score") == 0.0
