"""Warrants held at Scalable (ADR 0026) and the Discover fixes of 2026-11-20.

Scalable types a warrant DERIVATIVE; the mirror stored put warrants on
Anthropic as "etf" holdings, so the tax cockpit estimated them a
Vorabpauschale. Run b5a132a4's dossiers, checked against Yahoo and the web,
gave bond funds an equity-sized expected return and lost two labels.
"""
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import derivatives
from app.foundation.core.db import Base
from app.foundation.live_positions import asset_type_for
from app.foundation.models.entities import BrokerPosition, ConnectedAccount, DerivativeQuote, DerivativeTerms, User

TODAY = date(2026, 11, 20)
PUT = "DE000BE4QXD6"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _book(db, *, terms: bool = True, expiry: date = date(2026, 12, 23)) -> str:
    user = User(id=str(uuid4()), username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    depot = ConnectedAccount(user_id=user.id, source="scalable", name="Scalable depot", account_type="depot",
                             balance=Decimal("0"))
    db.add(depot)
    db.flush()
    db.add(BrokerPosition(
        user_id=user.id, connected_account_id=depot.id, source="scalable", isin=PUT, ticker=None,
        name="Anthropic Put 100,00 VON", security_type="DERIVATIVE", quantity=Decimal("10"),
        avg_buy_price=Decimal("1.5"), current_price=Decimal("1.725"), current_value=Decimal("17.25"),
    ))
    if terms:
        db.add(DerivativeTerms(user_id=user.id, isin=PUT, kind="put", style="american", strike=Decimal("100"),
                               strike_currency="USD", ratio=Decimal("0.1"), expiry=expiry, underlying_symbol="ANT"))
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS bar_prices (symbol TEXT, ts TIMESTAMP, open REAL, high REAL, low REAL,"
        " close REAL, volume REAL, currency TEXT, provider TEXT)"
    ))
    db.execute(text("INSERT INTO bar_prices VALUES ('ANT', '2026-11-19 00:00:00', 1, 1, 1, 97.17, 1, 'USD', 't')"))
    db.commit()
    return user.id


@pytest.fixture(autouse=True)
def _fx(monkeypatch):
    # EUR per USD on 2026-11-20 (EURUSD 1.0966); no network in tests.
    monkeypatch.setattr(derivatives, "_eur_per_unit", lambda db, ccy, on: 1 / 1.0966)
    monkeypatch.setattr(derivatives, "_rate", lambda db: 0.02)


# --- the type the broker reports ---------------------------------------------------


def test_a_scalable_derivative_is_not_mirrored_as_a_fund():
    assert asset_type_for("DERIVATIVE") == "derivative"
    assert asset_type_for("WARRANT") == "derivative"
    assert asset_type_for("ETF") == "etf"


def test_the_name_gives_underlying_kind_strike_and_issuer():
    assert derivatives.parse_name("Anthropic Put 100,00 VON") == {
        "underlying_name": "Anthropic", "kind": "put", "strike": 100.0, "issuer": "Vontobel",
    }
    assert derivatives.parse_name("Nvidia Call 1.250,50 SG")["strike"] == 1250.5
    assert derivatives.parse_name("iShares Core MSCI World") is None


# --- pricing -------------------------------------------------------------------------


def test_european_prices_keep_put_call_parity():
    s, k, t, r, v = 97.17, 100.0, 33 / 365, 0.02, 1.5
    call = derivatives.black_scholes("call", s, k, t, r, v)
    put = derivatives.black_scholes("put", s, k, t, r, v)
    assert call - put == pytest.approx(s - k * np.exp(-r * t), abs=1e-9)


def test_an_american_put_is_worth_at_least_the_european_and_exercise():
    args = (80.0, 100.0, 0.5, 0.05, 0.3)
    american = derivatives.binomial_american("put", *args)
    assert american >= derivatives.black_scholes("put", *args) - 1e-9
    assert american >= 20.0 - 1e-9
    # Without dividends an American call is a European one.
    assert derivatives.binomial_american("call", *args) == pytest.approx(
        derivatives.black_scholes("call", *args), rel=0.01)


def test_implied_volatility_reproduces_the_price():
    price = derivatives.option_price("put", "american", 97.17, 100.0, 33 / 365, 0.02, 1.5)
    iv = derivatives.implied_vol(price, "put", "american", 97.17, 100.0, 33 / 365, 0.02)
    assert iv == pytest.approx(1.5, abs=1e-3)
    # Below what exercising now pays: no volatility explains it.
    assert derivatives.implied_vol(1.0, "put", "american", 80.0, 100.0, 0.5, 0.02) is None


def test_the_anthropic_put_reads_as_a_short_position_with_its_decay():
    a = derivatives.analyse(kind="put", style="american", strike=100.0, ratio=0.1, expiry=date(2026, 12, 23),
                            quantity=10, price_eur=1.725, spot=97.17, eur_per_unit=1 / 1.0966, rate=0.02,
                            today=TODAY)
    assert a["days_to_expiry"] == 33 and a["missing"] == []
    # 1.725 EUR x 1.0966 / 0.1 = 18.92 USD a share, about 150 % volatility (2026-11-20).
    assert 1.3 < a["implied_vol"] < 1.7
    assert -0.6 < a["delta"] < -0.3
    assert a["exposure_eur"] < 0
    assert a["theta_eur_per_day"] < 0
    assert a["break_even"] == pytest.approx(100 - 1.725 * 1.0966 / 0.1, rel=1e-6)
    # Intrinsic value: (100 - 97.17) x 0.1 USD; the rest of the 1.725 EUR is time value.
    assert a["intrinsic_eur"] == pytest.approx(2.83 * 0.1 / 1.0966, rel=1e-3)
    at_minus_30 = next(x for x in a["scenarios"] if x["move"] == -0.3)
    assert at_minus_30["value_eur"] == pytest.approx((100 - 97.17 * 0.7) * 1.0 / 1.0966, rel=1e-6)


def test_missing_inputs_are_named_not_guessed():
    a = derivatives.analyse(kind="put", style="american", strike=100.0, ratio=0.1, expiry=date(2026, 12, 23),
                            quantity=10, price_eur=1.725, spot=None, eur_per_unit=None, rate=0.02, today=TODAY)
    assert "the underlying's price" in a["missing"] and "implied_vol" not in a


# --- positions, terms, Control Center -------------------------------------------------


def test_a_held_warrant_shows_its_terms_and_analysis():
    db = _memory_db()
    uid = _book(db)
    [row] = derivatives.derivative_positions(db, uid, today=TODAY)
    assert row["isin"] == PUT and row["terms"]["ratio"] == 0.1
    assert row["pnl_eur"] == pytest.approx(17.25 - 15.0, rel=1e-6)
    assert row["analysis"]["spot"] == 97.17 and row["analysis"]["exposure_eur"] < 0
    assert row["estimate"] is True


def test_a_warrant_without_terms_asks_for_them():
    db = _memory_db()
    _book(db, terms=False)
    [item] = derivatives.attention(db, today=TODAY)
    assert item["severity"] == "warning" and item["title"].startswith("Enter the terms")


def test_an_expiry_within_a_week_is_flagged():
    db = _memory_db()
    _book(db, expiry=TODAY + timedelta(days=3))
    [item] = derivatives.attention(db, today=TODAY)
    assert "expires in 3 days" in item["title"]
    db2 = _memory_db()
    _book(db2)
    assert derivatives.attention(db2, today=TODAY) == []


def test_terms_are_only_accepted_for_a_held_warrant():
    from app.interface.api.derivatives import DerivativeTermsIn, put_terms

    db = _memory_db()
    uid = _book(db, terms=False)
    user = db.get(User, uid)
    body = DerivativeTermsIn(kind="put", strike=100, ratio=0.1, expiry=date(2026, 12, 23), underlying_symbol="ant")
    out = put_terms(body, isin=PUT, user=user, db=db)
    assert out["terms"]["underlying_symbol"] == "ANT" and out["terms"]["strike_currency"] == "USD"
    with pytest.raises(HTTPException) as err:
        put_terms(body, isin="IE00B4L5Y983", user=user, db=db)
    assert err.value.status_code == 404


def test_the_evening_snapshot_keeps_one_quote_per_warrant_and_day():
    db = _memory_db()
    _book(db)
    fetched = []
    listed = {"implied_vol": 1.2, "strike": 100.0, "expiry": date(2026, 12, 18)}
    for _ in range(2):
        report = derivatives.snapshot_quotes(
            db, today=TODAY, ingest=lambda s, start: fetched.append(s), listed=lambda *a: listed,
        )
    assert report == {"warrants": 1, "quotes": 1, "listed": 1}
    [quote] = db.query(DerivativeQuote).all()
    assert quote.listed_iv == 1.2 and quote.warrant_iv > quote.listed_iv
    assert fetched == ["ANT", "ANT"]


# --- Quantlab ---------------------------------------------------------------------------


def test_the_warrant_margin_is_the_price_over_the_listed_volatility():
    from app.lab.options_lab import warrant_margin

    row = {"isin": PUT, "kind": "put", "style": "american", "strike": 100.0, "underlying_close": 97.17,
           "days_to_expiry": 33, "warrant_iv": 1.5, "listed_iv": 1.2}
    out = warrant_margin([row, {**row, "warrant_iv": 1.4}])
    assert out["median_vol_gap"] == pytest.approx(0.25)
    assert out["median_price_premium"] > 0.1


def test_a_protective_put_caps_each_period_loss_and_costs_its_premium():
    from app.lab.options_lab import protective_put_backtest

    rng = np.random.default_rng(7)
    days = pd.bdate_range("2010-01-01", periods=252 * 6)
    prices = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.011, len(days)))), index=days)
    vol = pd.Series(0.2, index=days)
    r = protective_put_backtest(prices, vol, otm=0.05, rate=0.02)
    # Strike 5 % below, premium paid on top: no period loses much more than 5 %.
    assert r["protected"]["worst_period"] > -0.07
    assert r["protected"]["worst_period"] > r["index"]["worst_period"]
    assert r["premium_per_year"] > 0 and 0 <= r["matched_equity_weight"] <= 1.5
    with pytest.raises(ValueError):
        protective_put_backtest(prices.iloc[:50], vol.iloc[:50])


# --- Discover run b5a132a4 ----------------------------------------------------------------


def test_a_bond_fund_prior_uses_its_equity_beta_not_its_bond_beta():
    from app.decision.discover.dossier_writer import _resolve_annual_estimate

    scores = {
        "backtest_vs_benchmark": {"candidate_return_annual": 0.0448, "candidate_volatility_annual": 0.06,
                                  "window_years": 3.0},
        "momentum_quality": {"momentum_12_1m": 0.013, "volatility_6m": 0.05},
        # EMIG.DE: 1.19 against EUNA.DE (a bond index), 0.15 against MSCI World.
        "quant_signals": {"instrument_type": "bond", "market_beta_weekly": 1.1855, "equity_beta_weekly": 0.15},
    }
    out = _resolve_annual_estimate(None, None, "bond", scores, "EMIG.DE")
    comp = out["components"]
    assert comp["beta_raw"] == 0.15 and comp["beta_source"] == "weekly vs EUNL.DE"
    assert comp["prior_pct"] < 4.0
    # Without the equity beta there is no equity premium at all.
    scores["quant_signals"].pop("equity_beta_weekly")
    assert _resolve_annual_estimate(None, None, "bond", scores, "EMIG.DE")["components"]["beta"] == 0.0


def test_a_liquid_corporates_fund_is_a_bond_fund():
    from app.foundation.etf_universe import is_bond_fund

    assert is_bond_fund("UBS BBG US Liquid Corp 1-5 UCITS ETF USD dis")
    assert is_bond_fund("iShares $ High Yield Corp Bond UCITS ETF")
    assert not is_bond_fund("iShares Edge MSCI World Value Factor UCITS ETF")


def test_a_low_correlation_claim_needs_a_low_correlation():
    from app.decision.discover.thesis_checks import check_thesis

    thesis = ("IS3S.DE exhibits a 12-1 month price return of +46.6 % within a value style framework and shows "
              "low correlation to the portfolio. It returned +24.8 % a year over the last 3.1 years, +6.2 % a year "
              "more than EUNL.DE.")
    check = check_thesis(thesis, [], facts_text="", macro=None, direction="long", correlation=0.73, is_fund=True)
    assert "low correlation" not in check.thesis and check.thesis.startswith("It returned")
    assert "low correlation" in check_thesis(
        thesis, [], facts_text="", macro=None, direction="long", correlation=0.2, is_fund=True).thesis


def test_momentum_says_it_is_in_euro():
    from app.decision.discover.dossier_facts import build_facts

    scores = {"momentum_quality": {"momentum_12_1m": 0.714, "momentum_rank": 0.9, "price_basis": "eur"}}
    facts = {f.key: f.text for f in build_facts(
        scores, annual={}, expected_return_pct=None, horizon_months=6, fundamentals={}, macro=None)}
    assert facts["momentum"].startswith("a 12-1 month price return in EUR of +71.4 %")
    scores["momentum_quality"]["price_basis"] = "local"
    facts = {f.key: f.text for f in build_facts(
        scores, annual={}, expected_return_pct=None, horizon_months=6, fundamentals={}, macro=None)}
    assert facts["momentum"].startswith("a 12-1 month price return of")


def test_a_half_year_reporter_is_not_given_a_quarter(monkeypatch):
    from app.decision.discover import dossier_writer
    from app.foundation import market

    monkeypatch.setattr(market, "fundamentals", lambda db, s: {
        "source": "yfinance", "stale": False,
        "data": {"pe_ratio": 11.2, "revenue_growth": 0.107, "period_months": 6},
    })
    out = dossier_writer._fetch_fundamentals("1308.HK", None)
    assert out["period_months"] == 6


# --- Phase 1 pricing fixes (review 2026-11-25) -------------------------------------------------


def test_american_gamma_is_smooth_in_the_spot():
    """A CRR tree gave 0.056 / 0.021 at spots 100 / 101 against 0.035 / 0.034 for the European."""
    for spot in (98.0, 99.0, 100.0, 101.0, 102.0):
        am = derivatives.greeks("put", "american", spot, 100.0, 30 / 365, 0.02, 0.4)
        eu = derivatives.greeks("put", "european", spot, 100.0, 30 / 365, 0.02, 0.4)
        assert am["gamma"] == pytest.approx(eu["gamma"], rel=0.1)
        assert am["delta"] == pytest.approx(eu["delta"], abs=0.01)


def test_the_tree_matches_black_scholes_for_a_european_call():
    args = (100.0, 100.0, 30 / 365, 0.02, 0.4)
    assert derivatives.binomial_american("call", *args) == pytest.approx(derivatives.black_scholes("call", *args), rel=2e-3)


def test_vega_near_the_volatility_floor_is_per_point():
    g = derivatives.greeks("call", "european", 100.0, 100.0, 0.5, 0.02, 0.008)
    lo, hi = 0.0001, 0.018
    true = (derivatives.black_scholes("call", 100.0, 100.0, 0.5, 0.02, hi)
            - derivatives.black_scholes("call", 100.0, 100.0, 0.5, 0.02, lo)) / (hi - lo) * 0.01
    assert g["vega_per_point"] == pytest.approx(true, rel=0.05)


def test_a_short_dated_or_low_vol_american_option_prices_without_dividing_by_zero():
    """The Leisen-Reimer probabilities saturate at 0 or 1 there: fall back to Black-Scholes."""
    iv = derivatives.implied_vol(10.2, "put", "american", 90.0, 100.0, 7 / 365, 0.02)
    assert iv is None or iv > 0
    g = derivatives.greeks("call", "american", 100.0, 100.0, 0.5, 0.02, 0.005)
    assert set(g) == {"delta", "gamma", "theta_per_day", "vega_per_point"}
    far = derivatives.binomial_american("put", 90.0, 100.0, 7 / 365, 0.02, 0.2)
    assert far == pytest.approx(10.0, abs=0.1)
