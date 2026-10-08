"""ADR 0018 Phase 1: provenance stamps, the shadow ledger and the daily ledger.

Prices come from an injected EUR-close loader (no provider, no network); the
DB layer is a real in-memory SQLite session. The Postgres append-only triggers
cannot run on SQLite; they were exercised against a local Postgres when
migration 0130 was written.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.discover import shadow_ledger
from app.decision.discover.predictor import store_prediction
from app.decision.verification import candidate_outcomes, daily_ledger
from app.foundation import provenance
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    CandidateOutcome,
    DiscoverCandidateSnapshot,
    DiscoveryPrediction,
    TrustDailyActiveReturn,
    User,
)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db) -> str:
    user = User(username="ledger", password_hash="x")
    db.add(user)
    db.commit()
    return user.id


def _business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _business_days(date(2026, 9, 1), 80)  # Tue 2026-09-01 onwards
BENCH = "EUNL.DE"


class FakePrices:
    """``{symbol: {date: close}}`` behind the ledger loader signature."""

    def __init__(self, series: dict[str, dict[date, float]]):
        self.series = series
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, db, symbol, days, refresh):
        self.calls.append((symbol, refresh))
        return {d.isoformat(): c for d, c in self.series.get(symbol.upper(), {}).items()}


def _geometric(days: list[date], daily: float, start: float = 100.0) -> dict[date, float]:
    return {d: start * (1.0 + daily) ** i for i, d in enumerate(days)}


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_canonical_hash_ignores_key_order_and_cohort_tracks_the_spec():
    assert provenance.canonical_hash({"a": 1, "b": [1, 2]}) == provenance.canonical_hash({"b": [1, 2], "a": 1})
    one = provenance.cohort_id({"weights": {"momentum": 0.2}})
    assert one == provenance.cohort_id({"weights": {"momentum": 0.2}})
    assert one != provenance.cohort_id({"weights": {"momentum": 0.25}})
    assert one.startswith(f"c{provenance.PROVENANCE_SCHEMA_VERSION}-")


def test_code_sha_reads_the_checkout_and_the_override(monkeypatch):
    provenance.code_sha.cache_clear()
    try:
        sha = provenance.code_sha()
        assert sha is None or len(sha) == 40
        monkeypatch.setenv("QUANTFOLIO_CODE_SHA", "abc123")
        provenance.code_sha.cache_clear()
        assert provenance.code_sha() == "abc123"
    finally:
        monkeypatch.delenv("QUANTFOLIO_CODE_SHA", raising=False)
        provenance.code_sha.cache_clear()


def test_read_git_sha_follows_refs_and_packed_refs(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text("# pack-refs\n" + "f" * 40 + " refs/heads/main\n")
    assert provenance._read_git_sha(tmp_path) == "f" * 40
    (git / "refs" / "heads" / "main").write_text("e" * 40 + "\n")
    assert provenance._read_git_sha(tmp_path) == "e" * 40
    (git / "HEAD").write_text("d" * 40 + "\n")  # detached
    assert provenance._read_git_sha(tmp_path) == "d" * 40


def test_llm_identity_summarises_props_and_degrades_on_failure(monkeypatch):
    db = _memory_db()
    provenance._props_cache.clear()
    props = {
        "model_path": "/var/lib/quantfolio/models/Qwen3.5-9B-UD-Q4_K_XL.gguf",
        "build_info": "b6000-abc",
        "chat_template": "{{ messages }}",
        "default_generation_settings": {"n_ctx": 32768, "params": {"temperature": 0.7, "presence_penalty": 1.5}},
    }
    monkeypatch.setattr(provenance, "_probe_props", lambda root, headers: props)
    ident = provenance.llm_server_identity(db, refresh=True)
    assert ident["available"] is True
    assert ident["model_path"].endswith(".gguf")
    assert ident["server_sampling"] == {"temperature": 0.7, "presence_penalty": 1.5}
    assert ident["chat_template_sha256"] == provenance.canonical_hash("{{ messages }}")

    def boom(root, headers):
        raise ConnectionError("refused")

    monkeypatch.setattr(provenance, "_probe_props", boom)
    down = provenance.llm_server_identity(db, refresh=True)
    assert down["available"] is False and "refused" in down["error"]
    provenance._props_cache.clear()


def test_discover_stamp_keeps_the_llm_out_of_the_cohort(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(provenance, "llm_server_identity", lambda db=None, refresh=False: {"available": True, "model_path": "a"})
    first = shadow_ledger.discover_stamp(
        db, run_id="r1", signal_config={"weights": {"m": 1}}, signal_config_id="cfg",
        prompt_config={"p": 1}, horizon_days=21,
    )
    monkeypatch.setattr(provenance, "llm_server_identity", lambda db=None, refresh=False: {"available": True, "model_path": "b"})
    second = shadow_ledger.discover_stamp(
        db, run_id="r2", signal_config={"weights": {"m": 1}}, signal_config_id="cfg",
        prompt_config={"p": 2}, horizon_days=21,
    )
    assert first["cohort_id"] == second["cohort_id"]  # dossier model and prompt: details only
    third = shadow_ledger.discover_stamp(
        db, run_id="r3", signal_config={"weights": {"m": 2}}, signal_config_id="cfg",
        prompt_config={"p": 1}, horizon_days=21,
    )
    assert third["cohort_id"] != first["cohort_id"]
    assert first["dossier_llm"]["model_path"] == "a"
    assert first["provenance_schema_version"] == provenance.PROVENANCE_SCHEMA_VERSION


def test_store_prediction_persists_the_stamp():
    db = _memory_db()
    uid = _user(db)
    pred = store_prediction(
        db, symbol="SAP.DE", composite_score=0.7, signal_breakdown={}, user_id=uid, run_id="r1",
        price_at_prediction=100.0, provenance_json={"cohort_id": "c1-x"},
    )
    db.commit()
    assert db.get(DiscoveryPrediction, pred.id).provenance_json == {"cohort_id": "c1-x"}
    bare = store_prediction(db, symbol="SAP.DE", composite_score=0.7, signal_breakdown={}, user_id=uid, run_id="r1",
                            price_at_prediction=100.0)
    db.commit()
    assert db.get(DiscoveryPrediction, bare.id).provenance_json is None


def test_trust_headline_says_one_trading_day_in_the_singular():
    from app.decision.verification.trust import _days

    assert _days(1) == "1 trading day" and _days(2) == "2 trading days" and _days(1200) == "1,200 trading days"


# ---------------------------------------------------------------------------
# Shadow-ledger capture
# ---------------------------------------------------------------------------


def _result(symbol, composite, *, reject=None, source="screen_equity", name=None, sector="Tech", capped=False):
    out = {
        "symbol": symbol, "isin": None, "name": name or symbol, "source": source,
        "scores": {"sentiment_fundamentals": {"sector": sector}, "composite_raw": composite,
                   "composite_inputs": [{"signal": "momentum", "score": composite}]},
        "concerns": [], "reject_stage": reject, "reject_reason": "gate" if reject else None,
        "composite_score": composite if reject is None else 0.0,
    }
    if capped:
        out["sector_capped"] = True
    return out


def test_capture_snapshot_freezes_every_candidate_with_stock_ranks(monkeypatch):
    db = _memory_db()
    uid = _user(db)
    monkeypatch.setattr(shadow_ledger, "history", lambda db, s, days, allow_live: [
        {"date": date(2026, 10, 2), "close": 10.0}, {"date": date(2026, 10, 5), "close": 11.0},
    ])
    results = [
        _result("AAA", 0.7),
        _result("BBB", 0.9),
        _result("CCC", 0.5, capped=True),
        _result("DDD", 0.0, reject="history_ingest"),
        _result("IWDA.L", 0.95, source="screen_etf", name="iShares Core MSCI World UCITS ETF"),
    ]
    stamp = {"cohort_id": "c1-test"}
    issued = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
    n = shadow_ledger.capture_snapshot(
        db, run_id="run1", user_id=uid, issued_at=issued, pipeline_results=results,
        shortlisted_symbols={"BBB", "AAA", "IWDA.L"}, picked_symbols={"BBB", "IWDA.L"}, stamp=stamp,
    )
    db.commit()
    assert n == 5
    rows = {r.symbol: r for r in db.query(DiscoverCandidateSnapshot).all()}
    assert rows["BBB"].stock_rank == 1 and rows["AAA"].stock_rank == 2 and rows["CCC"].stock_rank == 3
    assert rows["DDD"].evaluable is False and rows["DDD"].composite is None and rows["DDD"].stock_rank is None
    assert rows["IWDA.L"].instrument_group == "etf" and rows["IWDA.L"].stock_rank is None
    assert rows["CCC"].sector_capped is True and rows["CCC"].shortlisted is False
    assert rows["BBB"].picked and not rows["AAA"].picked and rows["AAA"].shortlisted
    assert all(r.n_evaluable_stocks == 3 for r in rows.values())
    assert rows["AAA"].entry_close == 11.0 and rows["AAA"].entry_close_date == date(2026, 10, 5)
    assert rows["AAA"].issue_date == date(2026, 10, 5) and rows["AAA"].cohort_id == "c1-test"
    assert rows["AAA"].components_json["composite_inputs"][0]["signal"] == "momentum"
    # Append-only per run: a second capture writes nothing.
    assert shadow_ledger.capture_snapshot(
        db, run_id="run1", user_id=uid, issued_at=issued, pipeline_results=results,
        shortlisted_symbols=set(), picked_symbols=set(), stamp=stamp,
    ) == 0


# ---------------------------------------------------------------------------
# Daily ledger
# ---------------------------------------------------------------------------


def _prediction(db, uid, symbol, issued: date, *, direction="buy", portfolio_id=None, horizon=3):
    pred = store_prediction(
        db, symbol=symbol, composite_score=0.6, signal_breakdown={}, user_id=uid, run_id="r",
        direction_hint=direction, horizon_days=horizon, price_at_prediction=1.0, portfolio_id=portfolio_id,
    )
    pred.predicted_at = datetime.combine(issued, datetime.min.time(), tzinfo=UTC).replace(hour=5)
    db.commit()
    return pred


def test_rank_weights_long_short_unit_legs_and_ties():
    w = daily_ledger.rank_weights({"A": 0.9, "B": 0.5, "C": 0.1})
    assert w["A"] == pytest.approx(1.0) and w["C"] == pytest.approx(-1.0) and "B" not in w
    tied = daily_ledger.rank_weights({"A": 0.9, "B": 0.9, "C": 0.1, "D": 0.0})
    assert tied["A"] == pytest.approx(tied["B"])
    assert sum(v for v in tied.values() if v > 0) == pytest.approx(1.0)
    assert sum(v for v in tied.values() if v < 0) == pytest.approx(-1.0)


def test_ideas_series_enters_after_the_issue_close_and_measures_active_return():
    db = _memory_db()
    uid = _user(db)
    d0 = DAYS[0]
    _prediction(db, uid, "AAA", d0, horizon=3)
    prices = FakePrices({BENCH: _geometric(DAYS[:10], 0.01), "AAA": _geometric(DAYS[:10], 0.03)})
    written = daily_ledger.freeze_series(db, uid, "ideas", today=DAYS[9], loader=prices)
    db.commit()
    rows = db.query(TrustDailyActiveReturn).order_by(TrustDailyActiveReturn.day).all()
    assert written == len(rows) == 9  # every calendar day after the issue day
    by_day = {r.day: r for r in rows}
    # Open on the 3 trading days after d0, each worth +3 % - +1 %.
    for d in DAYS[1:4]:
        assert by_day[d].n_open == 1 and by_day[d].value == pytest.approx(0.02)
    for d in DAYS[4:10]:
        assert by_day[d].n_open == 0 and by_day[d].value is None
    assert all(r.benchmark == BENCH for r in rows)
    # Idempotent: nothing left to freeze.
    assert daily_ledger.freeze_series(db, uid, "ideas", today=DAYS[9], loader=prices) == 0


def test_advisor_series_flips_sells_and_skips_holds():
    db = _memory_db()
    uid = _user(db)
    _prediction(db, uid, "AAA", DAYS[0], direction="sell", portfolio_id="p1", horizon=2)
    _prediction(db, uid, "BBB", DAYS[0], direction="neutral", portfolio_id="p1", horizon=2)
    _prediction(db, uid, "CCC", DAYS[0], horizon=2)  # an ideas row: not the advisor's
    prices = FakePrices({
        BENCH: _geometric(DAYS[:5], 0.0), "AAA": _geometric(DAYS[:5], 0.02),
        "BBB": _geometric(DAYS[:5], 0.5), "CCC": _geometric(DAYS[:5], 0.5),
    })
    daily_ledger.freeze_series(db, uid, "advisor", today=DAYS[4], loader=prices)
    db.commit()
    row = db.query(TrustDailyActiveReturn).filter_by(series="advisor", day=DAYS[1]).one()
    assert row.n_open == 1 and row.value == pytest.approx(-0.02)


def test_a_day_waits_for_a_lagging_close_then_freezes_it_as_stale_cash():
    db = _memory_db()
    uid = _user(db)
    _prediction(db, uid, "AAA", DAYS[0], horizon=10)
    stock = _geometric(DAYS[:3], 0.01)  # stops trading after DAYS[2]
    prices = FakePrices({BENCH: _geometric(DAYS[:12], 0.01), "AAA": stock})
    # Today = DAYS[5]: DAYS[3] is only 2 trading days behind, so it waits.
    daily_ledger.freeze_series(db, uid, "ideas", today=DAYS[5], loader=prices)
    db.commit()
    days = sorted(r.day for r in db.query(TrustDailyActiveReturn).all())
    assert days == DAYS[1:3]
    # Past the grace window the carried price stands in: own return 0, active -1 %.
    daily_ledger.freeze_series(db, uid, "ideas", today=DAYS[11], loader=prices)
    db.commit()
    stale = db.query(TrustDailyActiveReturn).filter_by(day=DAYS[3]).one()
    assert stale.n_stale == 1 and stale.value == pytest.approx(-0.01)
    assert stale.detail_json["stale"] == ["AAA"]
    # Days still inside the grace window keep waiting.
    assert max(r.day for r in db.query(TrustDailyActiveReturn).all()) == DAYS[11 - daily_ledger.GRACE_TRADING_DAYS]


def _snapshots(db, uid, run_id, issued: date, composites: dict[str, float]):
    for symbol, composite in composites.items():
        db.add(DiscoverCandidateSnapshot(
            user_id=uid, run_id=run_id, issued_at=datetime.combine(issued, datetime.min.time(), tzinfo=UTC),
            issue_date=issued, symbol=symbol, instrument_group="stock", evaluable=True,
            composite=composite, components_json={}, n_evaluable_stocks=len(composites),
            shortlisted=False, sector_capped=False, picked=False, cohort_id="c1-x", provenance_json={},
        ))
    db.commit()


def test_ranking_series_is_the_rank_weighted_long_short_of_open_cohorts():
    db = _memory_db()
    uid = _user(db)
    n = daily_ledger.MIN_RANKING_STOCKS
    composites = {f"S{i:02d}": i / n for i in range(n)}
    _snapshots(db, uid, "run1", DAYS[0], composites)
    _snapshots(db, uid, "small", DAYS[0], {"X1": 0.9, "X2": 0.1})  # too few stocks: not a cohort
    # Better-ranked stocks rise faster: daily return proportional to composite.
    series = {BENCH: _geometric(DAYS[:30], 0.0)}
    for s, c in composites.items():
        series[s] = _geometric(DAYS[:30], 0.01 * c)
    prices = FakePrices(series)
    daily_ledger.freeze_series(db, uid, "ranking", today=DAYS[29], loader=prices)
    db.commit()
    rows = {r.day: r for r in db.query(TrustDailyActiveReturn).filter_by(series="ranking").all()}
    weights = daily_ledger.rank_weights(composites)
    expected = sum(w * 0.01 * composites[s] for s, w in weights.items())
    assert rows[DAYS[1]].n_open == 1 and rows[DAYS[1]].value == pytest.approx(expected)
    assert rows[DAYS[1]].value > 0
    assert rows[DAYS[daily_ledger.RANKING_HORIZON_DAYS]].n_open == 1
    assert rows[DAYS[daily_ledger.RANKING_HORIZON_DAYS + 1]].n_open == 0


# ---------------------------------------------------------------------------
# Candidate outcomes
# ---------------------------------------------------------------------------


def test_candidate_outcomes_resolve_wait_and_score_delistings():
    db = _memory_db()
    uid = _user(db)
    _snapshots(db, uid, "run1", DAYS[0], {"LIVE": 0.6, "GONE": 0.4, "NONE": 0.2})
    series = {
        BENCH: _geometric(DAYS[:70], 0.001),
        "LIVE": _geometric(DAYS[:70], 0.002),
        "GONE": _geometric(DAYS[:8], 0.002),  # stops trading after DAYS[7]
    }
    prices = FakePrices(series)
    today = DAYS[12]
    candidate_outcomes.resolve_candidate_outcomes(db, today=today, loader=prices)
    db.commit()
    got = {(o.symbol, o.horizon_days): o for o in db.query(CandidateOutcome).all()}
    assert set(got) == {("LIVE", 5), ("LIVE", 10), ("GONE", 5)}  # GONE@10 and NONE wait for the grace
    live5 = got[("LIVE", 5)]
    assert live5.status == "resolved" and live5.target_date == DAYS[5] and live5.entry_date == DAYS[0]
    assert live5.ret_eur == pytest.approx(1.002 ** 5 - 1)
    assert live5.bench_ret_eur == pytest.approx(1.001 ** 5 - 1)
    assert live5.excess_eur == pytest.approx(live5.ret_eur - live5.bench_ret_eur)

    later = DAYS[10] + timedelta(days=candidate_outcomes.GRACE_DAYS + 1)
    candidate_outcomes.resolve_candidate_outcomes(db, today=later, loader=prices)
    db.commit()
    got = {(o.symbol, o.horizon_days): o for o in db.query(CandidateOutcome).all()}
    assert got[("GONE", 10)].status == "delisted" and got[("GONE", 10)].exit_date == DAYS[7]
    assert got[("NONE", 5)].status == "no_price" and got[("NONE", 5)].ret_eur is None
    assert ("LIVE", 63) not in got  # horizon not reached yet
    before = db.query(CandidateOutcome).count()
    candidate_outcomes.resolve_candidate_outcomes(db, today=later, loader=prices)
    assert db.query(CandidateOutcome).count() == before  # append-only, no duplicates


# ---------------------------------------------------------------------------
# Exploratory backfill (ADR 0018 §5)
# ---------------------------------------------------------------------------


def test_backfill_rebuilds_past_runs_as_exploratory_rows_once():
    import json as _json

    from app.decision.discover import backfill_snapshots
    from app.decision.verification.daily_ledger import ranking_members
    from app.foundation.models.entities import DiscoverCandidate, DiscoverCandidateSnapshot, DiscoverRun

    db = _memory_db()
    user = User(username="bf", password_hash="x")
    db.add(user)
    db.commit()
    run = DiscoverRun(user_id=user.id, status="completed", stage_json="{}", params_json="{}",
                      created_at=datetime(2026, 5, 4, 5, tzinfo=UTC))
    db.add(run)
    db.flush()
    for i in range(40):
        status, stage = ("shortlisted", None) if i < 3 else ("rejected", "shortlist_limit")
        db.add(DiscoverCandidate(run_id=run.id, symbol=f"S{i:02d}", source="screen_index", status=status,
                                 reject_stage=stage, scores_json=_json.dumps({"composite": 1 - i / 100})))
    db.add(DiscoverCandidate(run_id=run.id, symbol="GONE", source="screen_index", status="rejected",
                             reject_stage="history_ingest", scores_json="{}"))
    db.add(DiscoveryPrediction(user_id=user.id, run_id=run.id, symbol="S00", predicted_at=run.created_at,
                               horizon_days=21, resolve_at=run.created_at, direction="buy", conviction=1.0,
                               features_json={}))
    db.add(DiscoverRun(user_id=user.id, status="failed", stage_json="{}", params_json="{}"))
    db.commit()

    assert backfill_snapshots(db, user_id=user.id) == {"runs": 1, "rows": 41}
    db.commit()
    assert backfill_snapshots(db, user_id=user.id) == {"runs": 0, "rows": 0}

    rows = {r.symbol: r for r in db.query(DiscoverCandidateSnapshot).all()}
    assert all(r.backfilled and r.cohort_id == "legacy_unstamped" for r in rows.values())
    assert rows["S00"].picked and rows["S00"].shortlisted and rows["S00"].stock_rank == 1
    assert rows["S05"].evaluable and not rows["S05"].shortlisted  # cut by the shortlist: still scored
    assert not rows["GONE"].evaluable and rows["GONE"].reject_stage == "history_ingest"
    assert rows["S00"].issue_date == date(2026, 5, 4) and rows["S00"].entry_close is None
    assert "backfilled" in rows["S00"].provenance_json["degradation_flags"]
    # Never part of the live ranking series.
    assert ranking_members(db, user.id) == []
