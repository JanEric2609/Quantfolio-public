"""Tests for the deterministic quant-signals stage (advisor-loop PR1, B2)."""
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import PriceCache
from app.decision.discover.pipeline import run_candidate_pipeline, stage_quant_signals
from app.foundation.instrument_taxonomy import classify_instrument


def _seed_prices(db, ticker: str, closes: list[float]) -> None:
    end = date.today()
    days: list[date] = []
    d = end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    now = datetime.now(UTC)
    for dd, close in zip(days, closes):
        db.add(
            PriceCache(
                id=uuid4().hex, ticker=ticker.upper(), date=dd,
                close=Decimal(str(round(close, 4))), fetched_at=now,
                source="test", stale=False, currency="EUR",
            )
        )
    db.commit()


def test_equity_quant_signals_computes_var_cvar():
    db = _memory_db()
    # Mildly rising series with wiggle → non-degenerate returns
    closes = [100.0 + i * 0.1 + (3.0 if i % 7 == 0 else 0.0) for i in range(200)]
    _seed_prices(db, "ACME", closes)

    scores, reject = stage_quant_signals(db, "ACME", is_etf=False)
    assert reject is None
    assert scores is not None
    assert scores["instrument_type"] == "equity"
    # Positive loss-magnitude convention; CVaR (tail mean) >= VaR (tail edge)
    assert scores["var_95_daily"] >= 0.0
    assert scores["cvar_95_daily"] >= scores["var_95_daily"] - 1e-9


def test_etf_skips_single_name_factor_regression():
    """ETF candidates must not crash on stock-only features; factor betas stay None."""
    db = _memory_db()
    closes = [50.0 + i * 0.05 + (1.0 if i % 5 == 0 else 0.0) for i in range(200)]
    _seed_prices(db, "VWCE.DE", closes)

    with patch("app.foundation.quant_factors.get_ff3_returns") as mock_ff:
        scores, reject = stage_quant_signals(db, "VWCE.DE", is_etf=True)
        mock_ff.assert_not_called()

    assert reject is None
    assert scores is not None
    assert scores["instrument_type"] == "etf"
    assert scores["factor_betas"] is None


def test_no_data_is_hard_failure():
    db = _memory_db()
    scores, reject = stage_quant_signals(db, "NODATA")
    assert scores is None
    assert reject is not None and "quant_signals" in reject


def test_resolve_instrument_type_source_authoritative():
    assert classify_instrument("ANY", source="screen_etf") == "etf"
    assert classify_instrument("UNKNOWN_TICKER_X", source="screen_index") == "equity"


def _consume_pipeline(generator):
    """Consume a run_candidate_pipeline generator and return its StopIteration value."""
    try:
        while True:
            next(generator)
    except StopIteration as exc:
        return exc.value


def test_bond_fund_keeps_bond_label():
    """O4 regression: a duration-bearing bond fund (CBE3.L-style) must keep
    ``instrument_type == "bond"`` in the pipeline's quant_signals scoring dict.

    stage_quant_signals writes only the binary ``etf|equity`` label; the
    post-stage patch in run_candidate_pipeline must restore the finer
    classify_instrument result. It restored ``money_market`` only, so bond
    funds fell through as "etf" and inherited equity anchors/shrinkage
    downstream (discover-maths audit O4).
    """
    db = _memory_db()
    closes = [100.0 + i * 0.05 + (1.0 if i % 5 == 0 else 0.0) for i in range(200)]
    _seed_prices(db, "CBE3.L", closes)

    candidate = {
        "symbol": "CBE3.L",
        "name": "iShares Core Global Aggregate Bond UCITS ETF",
        "isin": "IE00BDBRDM35",
        "source": "screen_etf",
    }
    # Sanity: the classifier itself resolves this candidate to "bond" —
    # the label loss happens between here and scores["quant_signals"].
    assert classify_instrument(candidate["symbol"], candidate["source"], candidate["name"]) == "bond"

    with ExitStack() as stack:
        stack.enter_context(patch("app.decision.discover.pipeline._preingest_candidates"))
        mock_gate_cls = stack.enter_context(
            patch("app.decision.discover.pipeline.MacroRegimeGate")
        )
        mock_gate = MagicMock()
        mock_gate.blocks_discovery.return_value = False
        mock_gate_cls.return_value = mock_gate

        # Every stage except quant_signals is patched: the REAL
        # stage_quant_signals runs against the seeded prices and emits its
        # binary etf|equity label, exactly as in production.
        for stage_name, ret in [
            ("stage_history_ingest", {"concerns": []}),
            ("stage_alpha_miner", {"ic": 0.0, "icir": 0.0, "n_obs": 0, "concerns": []}),
            ("stage_alpha_screener", {"regime_affinity": 0.5, "concerns": []}),
            (
                "stage_sentiment_fundamentals",
                {
                    "analyst_estimate_score": 0.5,
                    "sentiment_score": 0.5,
                    "fundamentals_score": 0.5,
                    "concerns": [],
                },
            ),
            (
                "stage_momentum_quality",
                {"momentum_12_1m": 0.01, "volatility_6m": 0.05, "concerns": []},
            ),
            (
                "stage_backtest_vs_benchmark",
                {"excess_return_annual": 0.0, "sharpe_delta": 0.0, "concerns": []},
            ),
            (
                "stage_verification_gate",
                {"sharpe": 0.2, "volatility": 0.05, "max_drawdown": 0.03, "concerns": []},
            ),
            ("stage_portfolio_fit", {"fit_score": 0.5, "concerns": []}),
            (
                "stage_estimate_revision_signal",
                {"sue": None, "revision_momentum": None, "dispersion": None, "concerns": []},
            ),
            (
                "stage_insider_signal",
                {"cluster_buy_score": None, "net_insider_flow_usd": None, "concerns": []},
            ),
        ]:
            stack.enter_context(
                patch(
                    f"app.decision.discover.pipeline.{stage_name}",
                    return_value=(ret, None),
                )
            )

        gen = run_candidate_pipeline(db, "user1", [candidate])
        shortlist = _consume_pipeline(gen)

    assert len(shortlist) == 1
    assert shortlist[0]["scores"]["quant_signals"]["instrument_type"] == "bond"


def _fake_factor_source(calls, n_days):
    """Factor returns on the last *n_days* weekdays, recording which region was asked for."""
    import numpy as np

    def fake(region, start=None, end=None):
        calls.append(region)
        rng = np.random.default_rng(7)
        days = []
        d = date.today()
        while len(days) < n_days:
            if d.weekday() < 5:
                days.append(str(d))
            d -= timedelta(days=1)
        factors = {
            name: {day: float(v) for day, v in zip(days, rng.normal(0, 0.01, len(days)))}
            for name in ("mkt_rf", "smb", "hml", "rmw", "cma")
        }
        return {"status": "completed", "factors": factors, "region": region}

    return fake


def test_factor_betas_use_the_listing_market():
    # ADR 0007 amendment: SPY loaded 0.50 on Europe's market factor and 0.99
    # on the US one; a US share needs the US factor file.
    import app.foundation.quant_factors as qf

    db = _memory_db()
    closes = [100.0 + i * 0.1 + (3.0 if i % 7 == 0 else 0.0) for i in range(260)]
    _seed_prices(db, "ACME", closes)
    _seed_prices(db, "ACME.DE", closes)
    _seed_prices(db, "ACME.T", closes)
    calls: list[str] = []
    with (
        patch.object(qf, "get_ff5_returns_for_listing", _fake_factor_source(calls, 300)),
        patch("app.foundation.market.usd_per_unit_by_date", return_value=None),
    ):
        us, _ = stage_quant_signals(db, "ACME", is_etf=False)
        eu, _ = stage_quant_signals(db, "ACME.DE", is_etf=False)
        jp, _ = stage_quant_signals(db, "ACME.T", is_etf=False)

    # A Tokyo listing used to count as a US one (no .T suffix known).
    assert calls == ["us", "europe", "japan"]
    assert us["factor_fit"]["region"] == "us"
    assert eu["factor_fit"]["region"] == "europe"
    assert jp["factor_fit"]["region"] == "japan"
    assert us["factor_betas"] is not None
    assert us["factor_fit"]["n_obs"] >= 120
    # Without a rate the EUR returns are regressed as they are, and say so.
    assert us["factor_fit"]["returns_in"] == "USD"
    assert eu["factor_fit"]["returns_in"] == "EUR"


def test_factor_regression_restates_local_returns_in_usd():
    # Ken French's factors are USD returns (ADR 0007 amendment 2).
    import app.foundation.quant_factors as qf

    db = _memory_db()
    closes = [100.0 + i * 0.1 + (3.0 if i % 7 == 0 else 0.0) for i in range(260)]
    _seed_prices(db, "ACME.DE", closes)
    days = [str(date.today() - timedelta(days=i)) for i in range(400)]
    with (
        patch.object(qf, "get_ff5_returns_for_listing", _fake_factor_source([], 300)),
        patch("app.foundation.market.usd_per_unit_by_date", return_value={d: 1.1 for d in days}) as rates,
    ):
        eu, _ = stage_quant_signals(db, "ACME.DE", is_etf=False)

    rates.assert_called_once()
    assert rates.call_args.args[1] == "EUR"
    assert eu["factor_fit"]["returns_in"] == "USD"


def test_factor_betas_need_half_a_year_of_overlap():
    import app.foundation.quant_factors as qf

    db = _memory_db()
    closes = [100.0 + i * 0.1 + (3.0 if i % 7 == 0 else 0.0) for i in range(260)]
    _seed_prices(db, "ACME", closes)
    with patch.object(qf, "get_ff5_returns_for_listing", _fake_factor_source([], 60)):
        scores, _ = stage_quant_signals(db, "ACME", is_etf=False)

    assert scores["factor_betas"] is None
    assert scores["factor_fit"] is None
