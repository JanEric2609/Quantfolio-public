"""Regression tests for the fixes of audit 2026-11-15 (docs/audit-2026-11-15.md)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from conftest import _memory_db

from app.foundation.models.entities import (
    AdvisorStrategy,
    Asset,
    DkbAccount,
    DkbPosition,
    LlmPortfolioDecision,
    PaperHolding,
    PaperPortfolio,
    PaperTrade,
    TaxLot,
    User,
)

IWDA_ISIN = "IE00B4L5Y983"


def _user(db, name: str = "audit") -> User:
    user = User(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _two_listings(db) -> None:
    """EUNL.DE and IWDA.L share one ISIN, as on prod."""
    db.add(Asset(isin=IWDA_ISIN, symbol="IWDA.L", exchange="LSE", currency="USD", name="IWDA", asset_type="etf",
                 ucits=True))
    db.add(Asset(isin=IWDA_ISIN, symbol="EUNL.DE", exchange="XETRA", currency="EUR", name="EUNL", asset_type="etf",
                 ucits=True))
    db.commit()


# --- 2.1 shared ISINs ------------------------------------------------------------------


def test_find_asset_tolerates_two_listings_and_prefers_the_held_symbol():
    from app.foundation.asset_lookup import find_asset

    db = _memory_db()
    _two_listings(db)
    assert find_asset(db, isin=IWDA_ISIN).symbol == "EUNL.DE"  # the EUR listing
    assert find_asset(db, isin=IWDA_ISIN, symbol="iwda.l").symbol == "IWDA.L"
    assert find_asset(db, isin="XX0000000000", symbol="EUNL.DE").symbol == "EUNL.DE"
    assert find_asset(db, isin="XX0000000000", symbol="EUNL.DE", symbol_fallback=False) is None


def test_paper_name_and_type_lookups_survive_a_shared_isin():
    from app.decision.paper_portfolio import resolve_instrument_name, resolve_paper_asset_type

    db = _memory_db()
    _two_listings(db)
    db.add(Asset(isin=None, symbol="DUP", exchange="A", currency="EUR", name="Dup A", asset_type="stock"))
    db.add(Asset(isin=None, symbol="DUP", exchange="B", currency="USD", name="Dup B", asset_type="stock"))
    db.commit()
    assert resolve_instrument_name(db, "EUNL.DE", isin=IWDA_ISIN) == "EUNL"
    assert resolve_paper_asset_type(db, "DUP") == "stock"


def test_a_crashed_review_is_journalled():
    from app.decision.llm_portfolio.jobs import _journal_failure

    db = _memory_db()
    user = _user(db)
    _journal_failure(db, user.id, "B", RuntimeError("boom"))
    row = db.query(LlmPortfolioDecision).one()
    assert row.status == "review_failed"
    assert row.mandate == "B"
    assert "boom" in (row.error or "")
    assert json.loads(row.decision_json) == {}


# --- 2.4 job results ---------------------------------------------------------------------


def test_job_tally_outcomes():
    from app.foundation.jobs import JobTally

    tally = JobTally()
    assert tally.outcome()["status"] == "ok"
    tally.success()
    tally.failure("user 1", ValueError("x"))
    out = tally.outcome()
    assert out["status"] == "degraded" and out["failed"] == 1 and "ValueError" in out["reason"]
    only_failures = JobTally()
    only_failures.failure("user 1")
    assert only_failures.outcome()["status"] == "failed"


def test_alphacrafter_and_prefetch_outcomes():
    from app.decision.discover.jobs import prefetch_job_outcome
    from app.lab.alphacrafter.jobs import alphacrafter_job_outcome

    assert alphacrafter_job_outcome({"stages": {"miner": {"status": "ok"}}})["status"] == "ok"
    degraded = alphacrafter_job_outcome({"stages": {"data": {"status": "error", "reason": "no bars"}}})
    assert degraded["status"] == "degraded" and "no bars" in degraded["reason"]
    assert alphacrafter_job_outcome({"error": "An internal error occurred"})["status"] == "failed"
    assert prefetch_job_outcome({"symbols": 100, "bars_failed": 5})["status"] == "ok"
    assert prefetch_job_outcome({"symbols": 100, "bars_failed": 40})["status"] == "degraded"
    assert prefetch_job_outcome({"symbols": 10, "bars_failed": 0, "stopped_early": True})["status"] == "degraded"


# --- 2.2 per-loop semaphore -------------------------------------------------------------


def test_the_shared_async_limiter_works_in_every_event_loop():
    from app.foundation.providers.rate_limiter import get_shared_async_limiter

    async def contend() -> int:
        async def hold() -> int:
            async with get_shared_async_limiter("council:test"):
                await asyncio.sleep(0.005)
                return 1

        return sum(await asyncio.gather(hold(), hold(), hold()))

    assert asyncio.run(contend()) == 3
    assert asyncio.run(contend()) == 3  # raised "bound to a different event loop" before


# --- 2.10 / 2.11 tax ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"freistellungsauftrag_amount": 1000, "tax_spouse_allowance": False}, Decimal("1000")),
        ({"freistellungsauftrag_amount": 1000, "tax_spouse_allowance": True}, Decimal("2000")),
        ({"freistellungsauftrag_amount": 1500, "tax_spouse_allowance": True}, Decimal("1500")),
        ({"freistellungsauftrag_amount": None, "tax_spouse_allowance": True}, Decimal("2000")),
    ],
)
def test_sparer_pauschbetrag_from_settings(settings, expected):
    from app.foundation.tax_calc import sparer_pauschbetrag_from_settings

    assert sparer_pauschbetrag_from_settings(settings) == expected


def _sale_gain(monkeypatch, *, is_fund: bool | None, bucket: str) -> Decimal:
    from app.foundation import tax_cockpit

    monkeypatch.setattr(
        tax_cockpit, "_vorab_per_unit_by_year",
        lambda db, symbol, until_year: {2023: Decimal("2"), 2024: Decimal("2")},
    )
    db = _memory_db()
    user = _user(db)
    db.add(TaxLot(user_id=user.id, isin="US0378331005", symbol="AAPL", fund_class="other",
                  teilfreistellung_pct=Decimal("0"), acquired_at=date(2022, 6, 1), quantity_initial=Decimal("10"),
                  quantity_remaining=Decimal("10"), cost_basis_eur=Decimal("1000")))
    db.commit()
    event = tax_cockpit.record_sale_via_fifo(
        db, user.id, isin="US0378331005", sell_date=date(2025, 3, 3), sell_quantity=Decimal("10"),
        sell_price_per_unit_eur=Decimal("150"), fund_class="other", bucket=bucket, is_fund=is_fund,
    )
    return Decimal(str(event.realised_gain_eur))


def test_a_share_sale_gets_no_vorabpauschale_credit(monkeypatch):
    share = _sale_gain(monkeypatch, is_fund=False, bucket="aktien")
    inferred = _sale_gain(monkeypatch, is_fund=None, bucket="aktien")
    fund = _sale_gain(monkeypatch, is_fund=True, bucket="sonstige")
    assert share == inferred == Decimal("500")
    assert fund < share


def test_multi_asset_and_reit_funds_are_not_aktienfonds():
    from app.foundation.fund_class import class_from_etf_universe_hint

    assert class_from_etf_universe_hint("Vanguard LifeStrategy 60% Equity UCITS ETF") == "misch"
    assert class_from_etf_universe_hint("iShares Developed Markets Property Yield UCITS ETF") == "other"
    assert class_from_etf_universe_hint("iShares Core MSCI World UCITS ETF") == "aktien"
    assert class_from_etf_universe_hint("iShares Core Euro Govt Bond UCITS ETF") == "other"


# --- 2.12 ECB cache ---------------------------------------------------------------------------


def test_ecb_rates_are_fetched_again_for_a_day_after_the_last_fixing(monkeypatch):
    from app.foundation import ecb_fx

    ecb_fx.clear_cache()
    calls: list[int] = []
    this_year = date.today().year
    fixings = {date(this_year, 1, 2): 1.10}

    def fetch(ccy: str, year: int) -> dict[date, float]:
        calls.append(year)
        return dict(fixings)

    monkeypatch.setattr(ecb_fx, "_fetch_year", fetch)
    monkeypatch.setattr(ecb_fx, "REFRESH_SECONDS", 0)
    assert ecb_fx.ecb_eur_per_unit("USD", date(this_year, 1, 2)) == pytest.approx(1 / 1.10)
    fixings[date(this_year, 1, 9)] = 1.20
    assert ecb_fx.ecb_eur_per_unit("USD", date(this_year, 1, 9)) == pytest.approx(1 / 1.20)
    assert len(calls) == 2
    # A day already covered is served from the cache.
    ecb_fx.ecb_eur_per_unit("USD", date(this_year, 1, 2))
    assert len(calls) == 2
    ecb_fx.clear_cache()


# --- 2.5 split detection ----------------------------------------------------------------------


def test_a_one_to_79_split_is_a_split_signature():
    from app.foundation.data_backbone.bar_guard import guard_bars

    days = pd.bdate_range("2026-10-20", periods=6)
    closes = [350.0, 349.8, 4.4275, 4.43, 4.41, 4.45]
    frame = pd.DataFrame({"ts": days, "close": closes, "provider": "yfinance"})
    result = guard_bars(frame, symbol="LYBK.DE", guard_class="equity_etf", allow_split=True, check_calendar=False)
    assert result.split_suspected
    assert result.split_days and result.split_days[0][1] == pytest.approx(4.4275 / 349.8)


# --- 2.6 advisor ISINs -------------------------------------------------------------------------


def _paper(db, user) -> PaperPortfolio:
    p = PaperPortfolio(user_id=user.id, name="Advisor", initial_cash=Decimal("1000"), baseline_value=Decimal("1000"),
                       mandate="advisor", managed_by="advisor", inception_at=datetime.now(UTC) - timedelta(days=5))
    db.add(p)
    db.commit()
    return p


def test_a_trade_without_an_isin_takes_the_stored_one():
    from app.decision.paper_portfolio import execute_trade

    db = _memory_db()
    user = _user(db)
    p = _paper(db, user)
    db.add(Asset(isin="IT0003132476", symbol="ENI.MI", exchange="MIL", currency="EUR", name="Eni", asset_type="stock"))
    db.commit()
    booked = execute_trade(db, p.id, "ENI.MI", "buy", 2, 14.0)
    db.commit()
    assert db.get(PaperTrade, booked["id"]).isin == "IT0003132476"
    assert db.query(PaperHolding).filter_by(portfolio_id=p.id).one().isin == "IT0003132476"


def test_holdings_without_an_isin_are_backfilled():
    from app.decision.paper_portfolio import backfill_holding_isins

    db = _memory_db()
    user = _user(db)
    p = _paper(db, user)
    db.add(PaperHolding(portfolio_id=p.id, ticker="TTE.PA", name="TotalEnergies", isin=None, quantity=Decimal("1"),
                        avg_buy_price=Decimal("60"), currency="EUR"))
    db.add(Asset(isin="FR0000120271", symbol="TTE.PA", exchange="PAR", currency="EUR", name="TotalEnergies",
                 asset_type="stock"))
    db.commit()
    assert backfill_holding_isins(db, allow_network=False) == 1
    assert db.query(PaperHolding).one().isin == "FR0000120271"


# --- 2.14 partial DKB sync ---------------------------------------------------------------------


def test_a_depot_whose_holdings_failed_keeps_its_positions(monkeypatch):
    from app.foundation.dkb import DkbSyncService

    monkeypatch.setattr("app.foundation.jobs.submit_job", lambda *a, **k: "stubbed")
    monkeypatch.setattr("app.foundation.portfolio.name_resolver._resolve_name_via_yfinance", lambda query: None)
    db = _memory_db()
    user = _user(db)
    depot = DkbAccount(user_id=user.id, type="depot", iban="DE02120300000000202051", balance=Decimal("0"))
    db.add(depot)
    db.flush()
    db.add(DkbPosition(account_id=depot.id, isin=IWDA_ISIN, name="IWDA", quantity=Decimal("10"),
                       current_price=Decimal("80"), current_value=Decimal("800")))
    db.commit()
    snapshot = {
        "accounts": [{"type": "depot", "iban": depot.iban, "balance": Decimal("0"), "currency": "EUR"}],
        "transactions": [],
        "positions": [],
        "holdings_failed_ibans": [depot.iban],
    }
    DkbSyncService().persist_snapshot(db, user.id, snapshot)
    assert db.query(DkbPosition).filter_by(account_id=depot.id).count() == 1
    snapshot["holdings_failed_ibans"] = []
    DkbSyncService().persist_snapshot(db, user.id, snapshot)
    assert db.query(DkbPosition).filter_by(account_id=depot.id).count() == 0


# --- 2.19 / 2.21 data layer ----------------------------------------------------------------------


def test_rss_items_sort_by_time_not_by_weekday_name():
    from app.foundation.rss_provider import _published_key

    items = [
        {"published": "Wed, 04 Nov 2026 08:00:00 GMT"},
        {"published": "Tue, 10 Nov 2026 08:00:00 GMT"},
        {"published": "2026-11-12T01:00:00Z"},
        {"published": "not a date"},
    ]
    ordered = [i["published"] for i in sorted(items, key=_published_key, reverse=True)]
    assert ordered == [
        "2026-11-12T01:00:00Z", "Tue, 10 Nov 2026 08:00:00 GMT", "Wed, 04 Nov 2026 08:00:00 GMT", "not a date",
    ]


def test_tel_aviv_and_johannesburg_quote_units():
    from app.foundation.providers.utils import currency_unit, listing_quote_currency

    assert currency_unit(listing_quote_currency("TEVA.TA")) == ("ILS", 100.0)
    assert currency_unit(listing_quote_currency("NPN.JO")) == ("ZAR", 100.0)
    assert listing_quote_currency("ANDG.US") == "USD"


def test_yahoo_has_no_us_suffix():
    from app.foundation.providers.yfinance_provider import _yahoo_symbol

    assert _yahoo_symbol("ANDG.US") == "ANDG"
    assert _yahoo_symbol("EUNL.DE") == "EUNL.DE"


# --- 2.20 strategies ----------------------------------------------------------------------------


def test_two_champions_do_not_break_every_later_lookup():
    from app.decision.advisor.strategy import get_champion

    db = _memory_db()
    user = _user(db)
    older = AdvisorStrategy(user_id=user.id, role="champion", config_json={},
                            promoted_at=datetime.now(UTC) - timedelta(days=2))
    newer = AdvisorStrategy(user_id=user.id, role="champion", config_json={}, promoted_at=datetime.now(UTC))
    db.add_all([older, newer])
    db.commit()
    assert get_champion(db, user.id).id == newer.id


# --- 2.23 / 2.25 / 2.26 maths -----------------------------------------------------------------------


def test_a_wiped_out_series_is_minus_one_hundred_percent():
    from app.decision.engine.metrics import annualised_return as engine_annualised
    from app.foundation.quant_metrics import annualised_return

    assert annualised_return([0.01, -1.0, 0.02]) == -1.0
    assert engine_annualised([0.01, -1.0]) == -1.0


def test_a_missing_benchmark_is_not_a_zero_return(monkeypatch):
    from app.foundation import recommendation_outcomes as ro

    def fake(db, ticker, start, end):
        return 0.015 if ticker != ro.BENCHMARK_TICKER else None

    monkeypatch.setattr(ro, "_fetch_return_for_ticker", fake)
    recent = datetime.now(UTC) - timedelta(days=ro.WINDOW_DAYS + 2)
    assert ro.evaluate_recommendation_outcome("AAPL", recent)["outcome_label"] == "pending"
    old = datetime.now(UTC) - timedelta(days=ro.WINDOW_DAYS + ro.PRICE_RETRY_DAYS + 5)
    assert ro.evaluate_recommendation_outcome("AAPL", old)["outcome_label"] == "unresolvable"


def test_the_promotion_drawdown_counts_a_first_day_loss():
    import numpy as np

    from app.decision.advisor.promotion import max_drawdown

    returns = [-0.05, 0.02]
    assert max_drawdown([1.0, *np.cumprod(1 + np.asarray(returns)).tolist()]) == pytest.approx(0.05)


# --- 2.15 cash in EUR ----------------------------------------------------------------------------------


def test_synced_usd_cash_is_converted(monkeypatch):
    from app.foundation import ecb_fx
    from app.foundation.live_positions import synced_cash
    from app.foundation.models.entities import ConnectedAccount

    monkeypatch.setattr(ecb_fx, "eur_per_unit", lambda db, ccy, on: (0.9, "ecb"))
    db = _memory_db()
    user = _user(db)
    db.add(ConnectedAccount(user_id=user.id, source="trading212", account_type="cash", name="T212 USD",
                            external_id="t212:usd", balance=Decimal("100"), currency="USD"))
    db.commit()
    total, synced = synced_cash(db, user.id)
    assert synced and total == Decimal("90.0")
