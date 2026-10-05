"""Tests for classify_instrument's money-market/etf/equity classification.

Regression coverage for the XEON.DE prod incident (2026-08-20): the type
system only ever returned "etf" or "equity", so nothing downstream
(composite scoring, MC calibration, LLM prompts, paper-trading asset_type)
could special-case a cash-equivalent instrument.

Also covers ``resolve_holding_asset_type`` (T3.1, DEC-E): the evidence-first
ladder that lets curated ``Asset`` rows override the heuristics at
holding-CREATION time.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import (
    Asset,
    DkbAccount,
    DkbPosition,
    PaperHolding,
    PaperPortfolio,
    User,
)
from app.foundation.instrument_taxonomy import (
    classify_instrument,
    resolve_holding_asset_type,
)


class TestResolveInstrumentType:
    def test_money_market_name_detected_regardless_of_source(self):
        assert (
            classify_instrument(
                "XEON.DE", source="unknown", name="Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C"
            )
            == "money_market"
        )

    def test_money_market_beats_screen_etf_source(self):
        """A money-market name must win even when source metadata says
        plain 'screen_etf' — the old code returned 'etf' unconditionally
        for that source, which is how XEON.DE got treated like a
        diversified-equity-basket ETF."""
        assert (
            classify_instrument(
                "XEON.DE", source="screen_etf", name="Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C"
            )
            == "money_market"
        )

    def test_screen_etf_source_without_money_market_name_stays_etf(self):
        assert classify_instrument("VWCE.DE", source="screen_etf", name="Vanguard FTSE All-World UCITS ETF") == "etf"

    def test_static_fallback_etf_without_name(self):
        assert classify_instrument("SPY", source=None) == "etf"

    def test_unknown_ticker_defaults_to_equity(self):
        assert classify_instrument("AAPL", source=None) == "equity"

    def test_no_name_provided_does_not_crash(self):
        assert classify_instrument("XEON.DE", source="unknown") == "equity"

    def test_bond_name_detected_regardless_of_source(self):
        """F17: CBE3.L (iShares EUR Govt Bond 1-3yr UCITS ETF) must classify
        as 'bond', not the generic 'etf' bucket."""
        assert (
            classify_instrument(
                "CBE3.L", source="screen_etf", name="iShares € Govt Bond 1-3yr UCITS ETF (Acc)"
            )
            == "bond"
        )

    def test_money_market_beats_bond_when_name_matches_both_patterns(self):
        """Money-market must be checked first — a name matching both
        keyword sets (e.g. contains "short term" and "estr") is a cash
        equivalent, not a bond fund."""
        assert (
            classify_instrument("XEON.DE", source="unknown", name="EUR Short Term Overnight ESTR Rate Fund")
            == "money_market"
        )


# ---------------------------------------------------------------------------
# Evidence-first ladder: resolve_holding_asset_type (T3.1, DEC-E)
# ---------------------------------------------------------------------------


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


ETF_ISIN = "IE00B4L5Y983"  # iShares Core MSCI World
XEON_ISIN = "IE00B53QG562"  # Xtrackers II EUR Overnight Rate Swap


def _asset(isin: str, asset_type: str) -> Asset:
    return Asset(
        isin=isin,
        symbol="SOME.SYM",
        exchange="XETRA",
        name="Curated Instrument Name",
        asset_type=asset_type,
        currency="EUR",
    )


class TestResolveHoldingAssetType:
    def test_isin_asset_evidence_beats_equity_heuristics(self):
        """An Asset row typed 'etf' wins even when symbol AND name would
        heuristically classify the instrument as equity — that override is
        the entire point of the ladder (prod: paper_holdings drifted to
        stock×12 vs etf×3 under heuristics alone)."""
        db = _memory_db()
        db.add(_asset(ETF_ISIN, "etf"))
        db.commit()

        resolved = resolve_holding_asset_type(
            db,
            isin=ETF_ISIN,
            symbol="MYSTK.XX",
            name="Mystery Industrial Holdings Inc.",
        )

        assert resolved == "etf"

    def test_isin_asset_evidence_beats_money_market_name(self):
        """Rung (a) is unconditional: an authoritative Asset type wins even
        against a money-market name that rung (b) would have matched."""
        db = _memory_db()
        db.add(_asset(XEON_ISIN, "stock"))
        db.commit()

        resolved = resolve_holding_asset_type(
            db,
            isin=XEON_ISIN,
            symbol="XEON.DE",
            name="Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C",
        )

        assert resolved == "stock"

    def test_fallback_money_market_name_without_asset_row(self):
        """No Asset evidence → classify_instrument exactly as before:
        XEON-style overnight-rate name still lands on money_market."""
        db = _memory_db()

        resolved = resolve_holding_asset_type(
            db,
            isin=XEON_ISIN,
            symbol="XEON.DE",
            name="Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C",
        )

        assert resolved == "money_market"

    def test_fallback_bond_name_without_asset_row(self):
        """No Asset evidence → CBE3.L-style duration-bearing fund name still
        lands on bond."""
        db = _memory_db()

        resolved = resolve_holding_asset_type(
            db,
            isin="IE00B3WJG912",
            symbol="CBE3.L",
            name="iShares € Govt Bond 1-3yr UCITS ETF (Acc)",
        )

        assert resolved == "bond"

    def test_fallback_screen_etf_source_without_asset_row(self):
        db = _memory_db()

        resolved = resolve_holding_asset_type(
            db,
            isin="IE00BK5BQT80",
            symbol="VWCE.DE",
            source="screen_etf",
            name="Vanguard FTSE All-World UCITS ETF",
        )

        assert resolved == "etf"

    def test_unknown_everything_fails_open_to_stock(self):
        """No ISIN, or an ISIN with no Asset row and no recognisable
        symbol/name: fails open to the Holding vocabulary's 'stock'."""
        db = _memory_db()

        assert resolve_holding_asset_type(db, isin=None, symbol="AAPL") == "stock"
        assert resolve_holding_asset_type(db, isin="", symbol="AAPL") == "stock"
        assert (
            resolve_holding_asset_type(
                db, isin="IE0000000000", symbol="UNKNOWN.XX", name="Unknowable Thing"
            )
            == "stock"
        )

    def test_no_isin_goes_straight_to_heuristics(self):
        db = _memory_db()

        assert resolve_holding_asset_type(db, symbol="SPY") == "etf"

    def test_duplicate_asset_rows_for_isin_do_not_crash(self):
        """The assets table allows several exchange/currency rows per ISIN;
        the ladder reads with .first() (propagate_asset_types convention)
        instead of .one_or_none(), so this must not raise."""
        db = _memory_db()
        db.add(Asset(
            isin=ETF_ISIN, symbol="IWDA", exchange="XETRA",
            name="iShares Core MSCI World", asset_type="etf", currency="EUR",
        ))
        db.add(Asset(
            isin=ETF_ISIN, symbol="SWDA", exchange="LSE",
            name="iShares Core MSCI World", asset_type="etf", currency="GBP",
        ))
        db.commit()

        assert resolve_holding_asset_type(db, isin=ETF_ISIN, symbol="IWDA") == "etf"


class TestMirrorIngestionUsesAssetEvidence:
    """Integration: _seed_holdings (the DKB→paper mirroring creation path)
    must land mirrored holdings with the Asset's authoritative type."""

    @staticmethod
    def _seed_depot(db):
        user = User(id="user-mirror", username="mirroruser", password_hash="hash")
        db.add(user)
        account = DkbAccount(user_id=user.id, type="depot", iban="DE02120300000000202051")
        db.add(account)
        portfolio = PaperPortfolio(user_id=user.id, mandate="A")
        db.add(portfolio)
        db.commit()
        return user, account, portfolio

    @staticmethod
    def _position(account_id: str, isin: str, ticker: str, name: str) -> DkbPosition:
        return DkbPosition(
            account_id=account_id,
            isin=isin,
            ticker=ticker,
            name=name,
            quantity=Decimal("10"),
            avg_buy_price=Decimal("100"),
            current_price=Decimal("110"),
            current_value=Decimal("1100"),
        )

    def test_seed_uses_asset_type_over_equity_heuristics(self):
        """A mirrored holding whose ISIN has an authoritative Asset row lands
        with the Asset's type — not the heuristic's — even though its ticker
        and name look like a plain stock."""
        from app.decision.paper_portfolio import _seed_holdings

        db = _memory_db()
        user, account, portfolio = self._seed_depot(db)
        db.add(_asset(ETF_ISIN, "etf"))
        db.add(self._position(account.id, ETF_ISIN, "MYSTK.XX", "Mystery Industrial Holdings Inc."))
        db.commit()

        _seed_holdings(db, portfolio.id, user.id)

        holding = db.query(PaperHolding).filter(PaperHolding.isin == ETF_ISIN).one()
        assert holding.asset_type == "etf"

    def test_seed_falls_back_to_name_heuristics_without_asset_row(self):
        """Without Asset evidence the pre-ladder behaviour is preserved:
        an XEON-style money-market name still types the holding correctly."""
        from app.decision.paper_portfolio import _seed_holdings

        db = _memory_db()
        user, account, portfolio = self._seed_depot(db)
        db.add(self._position(
            account.id, XEON_ISIN, "XEON.DE",
            "Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C",
        ))
        db.commit()

        _seed_holdings(db, portfolio.id, user.id)

        holding = db.query(PaperHolding).filter(PaperHolding.isin == XEON_ISIN).one()
        assert holding.asset_type == "money_market"


class TestResolvePaperAssetTypeDelegatesToLadder:
    """The manual-paper-portfolio creation helper shares the same ladder."""

    def test_isin_asset_row_wins_over_symbol_heuristics(self):
        from app.decision.paper_portfolio import resolve_paper_asset_type

        db = _memory_db()
        db.add(_asset(ETF_ISIN, "etf"))
        db.commit()

        assert resolve_paper_asset_type(db, "MYSTK.XX", isin=ETF_ISIN) == "etf"

    def test_no_evidence_fails_open_to_stock(self):
        from app.decision.paper_portfolio import resolve_paper_asset_type

        db = _memory_db()

        assert resolve_paper_asset_type(db, "UNKNOWNTICKER") == "stock"
