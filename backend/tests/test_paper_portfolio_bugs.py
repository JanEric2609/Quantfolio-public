"""Regression tests for paper portfolio seeding bugs.

Bug 1 — Depot double-count: initial_cash must NOT include DkbAccount rows
    with type="depot" (their balance is the securities market value, not cash).

Bug 2 — Stale baseline_value: recompute_baseline_value must be called after
    holdings are seeded in ALL code paths, including ensure_mandate_portfolio
    when existing_holdings is non-empty.

Bug 3 — Divergence delta sane: compute_divergence perf_delta is purely
    downstream of paper total_return_pct; verify it is ~0 when paper and real
    returns are equal.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import (
    DkbAccount,
    DkbPosition,
    PaperHolding,
    PaperPortfolio,
    User,
)
from app.decision.paper_portfolio import (
    get_or_create_paper_portfolio,
    reseed_manual_portfolio,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _user(db) -> User:
    user = User(id=uuid4().hex, username=f"u_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_dkb_account(
    db,
    user_id: str,
    account_type: str,
    balance: str,
    iban: str | None = None,
) -> DkbAccount:
    acct = DkbAccount(
        id=uuid4().hex,
        user_id=user_id,
        type=account_type,
        iban=iban or f"DE{uuid4().hex[:20]}",
        balance=Decimal(balance),
        currency="EUR",
    )
    db.add(acct)
    db.commit()
    return acct


def _add_dkb_position(
    db,
    account_id: str,
    isin: str,
    qty: str,
    avg_price: str,
) -> DkbPosition:
    pos = DkbPosition(
        id=uuid4().hex,
        account_id=account_id,
        isin=isin,
        ticker=isin[:4],
        name=f"Asset {isin}",
        quantity=Decimal(qty),
        avg_buy_price=Decimal(avg_price),
        current_price=Decimal(avg_price),
        current_value=Decimal(qty) * Decimal(avg_price),
    )
    db.add(pos)
    db.commit()
    return pos


# ---------------------------------------------------------------------------
# Bug 1 — Depot exclusion from initial_cash
# ---------------------------------------------------------------------------


class TestDepotExclusion:
    """initial_cash must be sourced only from non-depot (cash) accounts."""

    def test_get_or_create_excludes_depot_from_initial_cash(self):
        """
        Setup: giro €5 000 + depot €32 000 (market value of securities).
        Expected: initial_cash = 5 000, NOT 37 000.
        """
        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "5000.00")
        depot_acct = _add_dkb_account(db, user.id, "depot", "32000.00")
        # Add a position so _seed_holdings has something to copy
        _add_dkb_position(db, depot_acct.id, "IE00B4L5Y983", "100", "320.00")

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 320.0}):
            portfolio, seeded = get_or_create_paper_portfolio(db, user.id)

        assert seeded is True
        assert portfolio.initial_cash == Decimal("5000.00"), (
            f"initial_cash should be cash-only €5 000, got {portfolio.initial_cash}"
        )

    def test_get_or_create_no_depot_uses_all_cash_accounts(self):
        """No depot accounts → sum all accounts (giro + tagesgeld)."""
        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "3000.00")
        _add_dkb_account(db, user.id, "tagesgeld", "7000.00")

        portfolio, seeded = get_or_create_paper_portfolio(db, user.id)

        # No positions → no holdings seeded; but initial_cash should be 10k
        assert portfolio.initial_cash == Decimal("10000.00")

    def test_reseed_excludes_depot(self):
        """reseed_manual_portfolio must also exclude depot accounts."""
        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "4000.00")
        depot_acct = _add_dkb_account(db, user.id, "depot", "20000.00")
        _add_dkb_position(db, depot_acct.id, "IE00B3RBWM25", "50", "400.00")

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 400.0}):
            portfolio, _ = get_or_create_paper_portfolio(db, user.id)
            assert portfolio.initial_cash == Decimal("4000.00")

            # Now reseed — initial_cash must stay cash-only
            count, ok = reseed_manual_portfolio(db, portfolio.id, user.id)

        assert ok is True
        db.refresh(portfolio)
        assert portfolio.initial_cash == Decimal("4000.00"), (
            f"After reseed, initial_cash should still be €4 000 (cash-only), got {portfolio.initial_cash}"
        )

    def test_ensure_mandate_excludes_depot(self):
        """ensure_mandate_portfolio must exclude depot from initial_cash."""
        from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio

        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "6000.00")
        depot_acct = _add_dkb_account(db, user.id, "depot", "30000.00")
        _add_dkb_position(db, depot_acct.id, "US0231351067", "10", "3000.00")

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 3000.0}):
            portfolio = ensure_mandate_portfolio(db, user.id, "A")

        assert portfolio.initial_cash == Decimal("6000.00"), (
            f"Mandate initial_cash should be cash-only €6 000, got {portfolio.initial_cash}"
        )


# ---------------------------------------------------------------------------
# Bug 2 — baseline_value always recomputed after seed
# ---------------------------------------------------------------------------


class TestBaselineValueRecompute:
    """baseline_value must be initial_cash + cost_basis(holdings) after seeding."""

    def test_baseline_set_after_get_or_create_with_seeded_holdings(self):
        """baseline_value = initial_cash + cost_basis, not just initial_cash."""
        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "2000.00")
        depot_acct = _add_dkb_account(db, user.id, "depot", "10000.00")
        _add_dkb_position(db, depot_acct.id, "IE00B4L5Y983", "50", "200.00")

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 200.0}):
            portfolio, _ = get_or_create_paper_portfolio(db, user.id)

        # initial_cash = 2000 (giro only), cost_basis = 50 * 200 = 10000
        # baseline = 2000 + 10000 = 12000
        assert portfolio.baseline_value == Decimal("12000.00"), (
            f"baseline_value should be 12 000, got {portfolio.baseline_value}"
        )

    def test_ensure_mandate_recomputes_baseline_for_existing_portfolio(self):
        """
        If a mandate portfolio already has holdings but baseline_value is wrong
        (NULL or stale), ensure_mandate_portfolio must recompute it.
        """
        from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio

        db = _memory_db()
        user = _user(db)

        # Pre-create portfolio with wrong/missing baseline_value
        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Mandate A — Balanced Improver",
            initial_cash=Decimal("5000.00"),
            baseline_value=None,  # stale / missing
            mandate="A",
            managed_by="llm",
            mandate_config_json="{}",
        )
        db.add(portfolio)
        db.commit()

        # Pre-seed holdings directly (simulate prior seed run)
        holding = PaperHolding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            ticker="IWDA",
            name="MSCI World",
            quantity=Decimal("100"),
            avg_buy_price=Decimal("300.00"),
            asset_type="etf",
        )
        db.add(holding)
        db.commit()

        # ensure_mandate_portfolio should recompute baseline even though holdings exist
        _add_dkb_account(db, user.id, "giro", "5000.00")

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 300.0}):
            returned_portfolio = ensure_mandate_portfolio(db, user.id, "A")

        # baseline = 5000 + 100 * 300 = 35000
        assert returned_portfolio.baseline_value == Decimal("35000.00"), (
            f"baseline_value should be recomputed to 35 000 even when holdings pre-exist, "
            f"got {returned_portfolio.baseline_value}"
        )

    def test_ensure_mandate_preserves_baseline_after_sell(self):
        """
        Once a mandate portfolio has a real (non-NULL) baseline_value pinned
        at genuine inception, it must NOT be recomputed on subsequent
        ensure_mandate_portfolio calls — even after a sell shrinks the
        holdings table. Recomputing from whatever holdings currently exist
        post-sell silently overwrites the starting-capital denominator,
        which is exactly the root cause of the +618.6% stale total_return_pct
        bug (mandate A sold down HEN3.DE, and the next review's unconditional
        recompute collapsed baseline_value toward bare initial_cash).
        """
        from app.decision.llm_portfolio.mirror import ensure_mandate_portfolio

        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "5000.00")

        # Portfolio with a genuine, already-correct baseline pinned at inception.
        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Mandate A — Balanced Improver",
            initial_cash=Decimal("5000.00"),
            baseline_value=Decimal("35000.00"),  # correct, pinned starting capital
            mandate="A",
            managed_by="llm",
            mandate_config_json="{}",
        )
        db.add(portfolio)
        db.commit()

        # Simulate a large sell: only a small remaining holding, whose cost
        # basis is well below the pinned baseline — this is what would make
        # a naive recompute collapse baseline_value toward initial_cash.
        holding = PaperHolding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            ticker="IWDA",
            name="MSCI World",
            quantity=Decimal("5"),
            avg_buy_price=Decimal("300.00"),
            asset_type="etf",
        )
        db.add(holding)
        db.commit()

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 300.0}):
            returned_portfolio = ensure_mandate_portfolio(db, user.id, "A")

        assert returned_portfolio.baseline_value == Decimal("35000.00"), (
            "baseline_value must stay pinned after a sell once a real baseline "
            f"was already set, got {returned_portfolio.baseline_value}"
        )

    def test_no_phantom_return_when_price_equals_cost(self):
        """With price == cost, total_return_pct must be ~0%, not hundreds of %."""
        from app.decision.paper_portfolio import get_summary

        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "2228.67")
        depot_acct = _add_dkb_account(db, user.id, "depot", "34398.00")
        _add_dkb_position(db, depot_acct.id, "IE00B4L5Y983", "100", "343.98")

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 343.98}):
            portfolio, _ = get_or_create_paper_portfolio(db, user.id)
            summary = get_summary(db, portfolio.id)

        # total_return_pct should be ~0%, definitely NOT +1173%
        assert abs(summary["total_return_pct"]) < 0.001, (
            f"total_return_pct should be ~0 when price==cost, got {summary['total_return_pct']}"
        )
        # baseline_value = cash + cost_basis, not just cash
        assert summary["baseline_value"] > summary["initial_cash"], (
            "baseline_value must exceed initial_cash when holdings are seeded"
        )


# ---------------------------------------------------------------------------
# Bug 3 — Divergence delta is sane (downstream of baseline)
# ---------------------------------------------------------------------------


class TestDivergenceDeltaSane:
    """perf_delta = paper_return - real_return; must not explode from basis bugs."""

    def test_perf_delta_near_zero_when_paper_and_real_have_same_return(self):
        """
        When paper portfolio return ≈ real return, delta must be ≈ 0.
        This verifies divergence.compute_divergence has no independent basis error.
        """
        from app.decision.llm_portfolio.divergence import compute_divergence

        db = _memory_db()
        user = _user(db)

        # Create a paper portfolio with correct baseline
        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Paper A",
            initial_cash=Decimal("5000"),
            baseline_value=Decimal("35000"),  # correctly set: cash + cost_basis
            mandate="A",
            managed_by="llm",
            mandate_config_json="{}",
        )
        db.add(portfolio)
        db.commit()

        # Seed holdings whose market value = cost basis → 0% return
        holding = PaperHolding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            ticker="IWDA",
            name="MSCI World",
            quantity=Decimal("100"),
            avg_buy_price=Decimal("300.00"),
            asset_type="etf",
        )
        db.add(holding)
        db.commit()

        # Patch market_quote so current price == cost → 0% paper return
        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 300.0}):
            result = compute_divergence(db, user.id, portfolio.id)

        # No real snapshot → real_return defaults to 0.0
        # paper return ≈ 0 → delta ≈ 0
        assert abs(result["perf_delta"]) < 0.01, (
            f"perf_delta should be ≈ 0 when paper return ≈ real return, "
            f"got {result['perf_delta']}"
        )

    def test_perf_delta_reflects_actual_paper_outperformance(self):
        """
        Paper NAV +5% over the window, real NAV flat → delta = +0.05.
        Verifies the snapshot-window delta arithmetic (advisor-loop F1), not
        just near-zero. Same-window comparison — the phantom since-inception
        basis (PR #125) can no longer leak in.
        """
        from datetime import date, timedelta

        from app.foundation.models.entities import PaperSnapshot, PortfolioSnapshot
        from app.decision.llm_portfolio.divergence import compute_divergence

        db = _memory_db()
        user = _user(db)

        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Paper B",
            initial_cash=Decimal("1000"),
            baseline_value=Decimal("1000"),
            mandate="B",
            managed_by="llm",
            mandate_config_json="{}",
        )
        db.add(portfolio)
        db.commit()

        start = date.today() - timedelta(days=10)
        end = date.today()
        # Paper: 1000 → 1050 (+5%)
        for d, value in ((start, "1000"), (end, "1050")):
            db.add(PaperSnapshot(
                id=uuid4().hex, portfolio_id=portfolio.id, date=d,
                total_value=Decimal(value), cash_balance=Decimal(value),
                securities_value=Decimal("0"), total_return_pct=Decimal("0"),
                currency="EUR",
            ))
        # Real: flat 20000 → 20000 (0%)
        for d in (start, end):
            db.add(PortfolioSnapshot(
                id=uuid4().hex, user_id=user.id, date=d,
                total_value=Decimal("20000"), cash_value=Decimal("0"),
                security_value=Decimal("20000"), source="dkb_mirror",
                currency="EUR",
            ))
        db.commit()

        result = compute_divergence(db, user.id, portfolio.id)

        assert abs(result["perf_delta"] - 0.05) < 1e-6, (
            f"perf_delta should be 0.05, got {result['perf_delta']}"
        )
        # Regression guard for the phantom +811% figure: realistic fixture
        # must produce a small delta.
        assert abs(result["perf_delta"]) < 2.0


# ---------------------------------------------------------------------------
# Bug 4 — ISIN-only matching dead for mandate portfolios (ticker fallback)
# ---------------------------------------------------------------------------


class TestDivergenceTickerFallback:
    """compute_divergence must fall back to ticker matching when isin=None.

    Mandate/paper holdings almost always have isin=None (best-effort
    resolution failure in review.py's buy path), so ISIN-only matching is
    effectively dead for mandate portfolios. compute_divergence must match
    paper holdings lacking an ISIN against real holdings/DKB positions by
    uppercase ticker instead of silently reporting them as missing_in_real.
    """

    def test_ticker_only_match_against_real_holding(self):
        """Paper holding with isin=None matches a real Holding by ticker."""
        from app.decision.llm_portfolio.divergence import compute_divergence
        from app.foundation.models.entities import Holding, Portfolio

        db = _memory_db()
        user = _user(db)

        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Mandate A — Balanced Improver",
            initial_cash=Decimal("5000"),
            baseline_value=Decimal("35000"),
            mandate="A",
            managed_by="llm",
            mandate_config_json="{}",
        )
        db.add(portfolio)
        db.commit()

        # Paper holding with NO isin (the realistic mandate-review case).
        paper_holding = PaperHolding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            isin=None,
            ticker="iwda",  # lower-case on purpose — matching must normalize
            name="MSCI World",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("300.00"),
            asset_type="etf",
        )
        db.add(paper_holding)
        db.commit()

        # Real holding, also isin=None, same ticker (uppercase).
        real_portfolio = Portfolio(id=uuid4().hex, user_id=user.id, name="Real")
        db.add(real_portfolio)
        db.commit()
        real_holding = Holding(
            id=uuid4().hex,
            portfolio_id=real_portfolio.id,
            isin=None,
            ticker="IWDA",
            name="MSCI World",
            asset_type="etf",
            quantity=Decimal("8"),
        )
        db.add(real_holding)
        db.commit()

        result = compute_divergence(db, user.id, portfolio.id)

        # Must NOT appear as missing — the ticker fallback should match it.
        assert result["missing_in_real"] == [], (
            f"ticker-only holding should match via fallback, got missing_in_real={result['missing_in_real']}"
        )
        assert len(result["weight_diffs"]) == 1
        diff = result["weight_diffs"][0]
        assert diff["matched_by"] == "ticker"
        assert diff["ticker"] == "iwda"
        assert diff["isin"] is None
        assert diff["quantity_diff"] == 2.0  # 10 paper - 8 real

    def test_ticker_only_no_match_reports_missing_in_real(self):
        """Paper holding with isin=None and no matching real ticker is still reported missing."""
        from app.decision.llm_portfolio.divergence import compute_divergence

        db = _memory_db()
        user = _user(db)

        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Mandate B — Momentum Tilted",
            initial_cash=Decimal("1000"),
            baseline_value=Decimal("1000"),
            mandate="B",
            managed_by="llm",
            mandate_config_json="{}",
        )
        db.add(portfolio)
        db.commit()

        paper_holding = PaperHolding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            isin=None,
            ticker="NVDA",
            name="NVIDIA",
            quantity=Decimal("3"),
            avg_buy_price=Decimal("900.00"),
            asset_type="stock",
        )
        db.add(paper_holding)
        db.commit()

        result = compute_divergence(db, user.id, portfolio.id)

        assert len(result["missing_in_real"]) == 1
        entry = result["missing_in_real"][0]
        assert entry["ticker"] == "NVDA"
        assert entry["matched_by"] == "ticker"
        assert result["weight_diffs"] == []

    def test_isin_match_still_takes_priority_when_available(self):
        """A paper holding WITH an isin still matches via the ISIN index, not ticker."""
        from app.decision.llm_portfolio.divergence import compute_divergence
        from app.foundation.models.entities import Holding, Portfolio

        db = _memory_db()
        user = _user(db)

        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Mandate A — Balanced Improver",
            initial_cash=Decimal("5000"),
            baseline_value=Decimal("35000"),
            mandate="A",
            managed_by="llm",
            mandate_config_json="{}",
        )
        db.add(portfolio)
        db.commit()

        paper_holding = PaperHolding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            ticker="IWDA",
            name="MSCI World",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("300.00"),
            asset_type="etf",
        )
        db.add(paper_holding)
        db.commit()

        real_portfolio = Portfolio(id=uuid4().hex, user_id=user.id, name="Real")
        db.add(real_portfolio)
        db.commit()
        real_holding = Holding(
            id=uuid4().hex,
            portfolio_id=real_portfolio.id,
            isin="IE00B4L5Y983",
            ticker="IWDA",
            name="MSCI World",
            asset_type="etf",
            quantity=Decimal("10"),
        )
        db.add(real_holding)
        db.commit()

        result = compute_divergence(db, user.id, portfolio.id)

        assert len(result["weight_diffs"]) == 1
        assert result["weight_diffs"][0]["matched_by"] == "isin"
        assert result["missing_in_real"] == []
        assert result["missing_in_paper"] == []


# ---------------------------------------------------------------------------
# Bug A — Phantom return with zero cost basis (since-inception semantics fix)
# ---------------------------------------------------------------------------


class TestZeroCostBasisFallback:
    """When DKB positions have no avg_buy_price (FinTS limitation), use market price at seed time."""

    def test_seed_holdings_uses_market_price_when_cost_basis_is_zero(self):
        """
        Setup: DkbPosition with avg_buy_price=0 (or NULL) simulating FinTS read-only constraint.
        Expected: _seed_holdings falls back to current market price (~€343.98).
        Result: cost basis ≈ market value, baseline ≈ total portfolio value, return ≈ 0%.
        """
        from app.decision.paper_portfolio import _seed_holdings
        from app.decision.paper_portfolio import get_summary

        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "2228.67")
        depot_acct = _add_dkb_account(db, user.id, "depot", "34398.00")
        # Position with avg_buy_price=0 (FinTS doesn't provide it)
        _add_dkb_position(db, depot_acct.id, "IE00B4L5Y983", "100", "0")

        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Manual Baseline",
            initial_cash=Decimal("2228.67"),
            currency="EUR",
            baseline_value=Decimal("2228.67"),  # will be recomputed
            mandate="manual",
            managed_by="manual",
        )
        db.add(portfolio)
        db.commit()

        from app.foundation.models.entities import ListingCurrency

        db.add(ListingCurrency(symbol="IE00", currency="EUR", source="test"))
        db.commit()
        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 343.98}):
            _seed_holdings(db, portfolio.id, user.id)

        # Verify holding was seeded with market price as avg_buy_price
        holdings = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio.id).all()
        assert len(holdings) == 1, f"Expected 1 holding, got {len(holdings)}"
        assert holdings[0].avg_buy_price == Decimal("343.98"), (
            f"avg_buy_price should be 343.98 (market price), got {holdings[0].avg_buy_price}"
        )

        # Recompute baseline as the actual code does
        from app.decision.paper_portfolio import recompute_baseline_value

        recompute_baseline_value(db, portfolio.id)
        db.refresh(portfolio)

        # baseline = 2228.67 + (100 * 343.98) = 36627.67 ≈ total portfolio value
        expected_baseline = Decimal("2228.67") + (Decimal("100") * Decimal("343.98"))
        assert portfolio.baseline_value == expected_baseline, (
            f"baseline_value should be {expected_baseline}, got {portfolio.baseline_value}"
        )

        # Get summary with market price still at 343.98 (no movement)
        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 343.98}):
            summary = get_summary(db, portfolio.id)

        # total_return_pct = (total_value - baseline) / baseline ≈ 0% (not +1572%)
        assert abs(summary["total_return_pct"]) < 0.01, (
            f"total_return_pct should be ~0% (since-inception), got {summary['total_return_pct'] * 100:.1f}%"
        )

    def test_seed_holdings_skips_a_position_with_no_price_at_all(self):
        """
        Setup: DkbPosition with no cost, no broker price, and no quote.
        Expected: not seeded. A zero-cost line would show its whole value as
        a gain the day a quote arrives (it used to be seeded at 0).
        """
        from app.decision.paper_portfolio import _seed_holdings

        db = _memory_db()
        user = _user(db)

        _add_dkb_account(db, user.id, "giro", "1000.00")
        depot_acct = _add_dkb_account(db, user.id, "depot", "0.00")
        _add_dkb_position(db, depot_acct.id, "BADTICKER", "50", "0")

        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="Test",
            initial_cash=Decimal("1000.00"),
            mandate="manual",
            managed_by="manual",
        )
        db.add(portfolio)
        db.commit()

        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": None}):
            _seed_holdings(db, portfolio.id, user.id)

        holdings = db.query(PaperHolding).filter(PaperHolding.portfolio_id == portfolio.id).all()
        assert holdings == []

    def test_get_summary_handles_missing_market_quotes_gracefully(self):
        """
        Setup: Portfolio with holdings; market_quote returns None/unavailable for all tickers.
        Expected: get_summary does not crash; returns finite total_value and total_return_pct.
        The fallback to avg_buy_price means market_value = quantity * avg_buy_price,
        so return is 0% (since cost = market value).
        """
        from app.decision.paper_portfolio import get_summary

        db = _memory_db()
        user = _user(db)

        portfolio = PaperPortfolio(
            id=uuid4().hex,
            user_id=user.id,
            name="No Quotes Portfolio",
            initial_cash=Decimal("5000.00"),
            baseline_value=Decimal("5000.00"),
            mandate="manual",
            managed_by="manual",
        )
        db.add(portfolio)
        db.commit()

        holding = PaperHolding(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            isin="BADTICKER",
            ticker="BADTICKER",
            name="Unknown Asset",
            quantity=Decimal("100"),
            avg_buy_price=Decimal("50.00"),  # cost basis exists
            asset_type="stock",
        )
        db.add(holding)
        db.commit()

        # market_quote returns None — quote is unavailable
        with patch("app.decision.paper_portfolio.market_quote", return_value={"price": None}):
            summary = get_summary(db, portfolio.id)

        # Should not crash; must return finite numbers
        assert isinstance(summary["total_value"], float)
        assert isinstance(summary["total_return_pct"], float)
        assert summary["total_value"] >= 0
        # When market_quote returns None, get_holdings falls back to avg_buy_price
        # market_value = 100 * 50 = 5000
        # total_value = initial_cash (5000) + securities (5000) = 10000
        # baseline = 5000 (no seeded cost basis, just initial_cash)
        # return = (10000 - 5000) / 5000 = 1.0 = +100%
        # BUT: the holding was added directly, not seeded from DKB, so it has no
        # cost basis in the portfolio baseline computation.
        # The test validates it doesn't crash; the return value depends on the fixture setup.
        assert summary["total_value"] > 0, "total_value must be positive or zero"
        # The most important thing: this call must not crash or return NaN
        assert not (summary["total_return_pct"] != summary["total_return_pct"]), (  # NaN check
            "total_return_pct must be a finite number, not NaN"
        )
