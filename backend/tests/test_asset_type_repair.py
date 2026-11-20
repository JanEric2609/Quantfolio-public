"""Asset-type repair loop: Asset truth must reach existing Holding rows.

Production receipt: prod paper_holdings sat at stock×12 / money_market×2 /
etf×3 from mirror-seeded fail-open typing while the assets table held the
correct classification for the same ISINs. ``classify_and_enrich`` upserted
only Asset rows, so pre-existing Holdings kept their stale ``asset_type``
forever (the one correct prod holdings row was right by creation-order luck).

Locked here (DEC-E: auto-repair where ISIN↔Asset evidence exists, unknowns
untouched):

1. ``classify_and_enrich`` propagates each classified Asset's type onto every
   Holding row with a matching ISIN — ``dkb_sync`` and manual alike.
2. Manual holdings WITHOUT an ISIN are never touched.
3. ``sync_dkb_positions_to_holdings`` re-types an existing holding on the
   UPDATE path when its Asset's classification changed (row identity stable).
4. ``propagate_asset_types`` returns per-ISIN changed-row counts and is a
   no-op on the second call (Wave-2 admin repair endpoint reuses it).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import _memory_db

from app.foundation.models.entities import Asset, DkbAccount, Holding, Portfolio, User
from app.foundation.dkb import DkbSyncService
from app.foundation.etf_classification import classify_and_enrich, propagate_asset_types
from app.foundation.portfolio_service import sync_dkb_positions_to_holdings

ETF_ISIN = "IE00B4L5Y983"
STOCK_ISIN = "US0378331005"


def _raise_offline(*args: object, **kwargs: object) -> None:
    raise RuntimeError("offline test guard")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch):
    """Hard-offline guards over every network seam these pipelines can touch."""
    monkeypatch.setattr("app.foundation.jobs.submit_job", lambda *a, **k: "stubbed")
    monkeypatch.setattr(
        "app.foundation.portfolio.name_resolver._resolve_name_via_yfinance", lambda query: None,
    )
    monkeypatch.setattr(
        "app.foundation.etf_classification.EtfUniverseProvider.lookup_by_isin", _raise_offline,
    )


def _seed_user_and_portfolio(db):
    user = User(id="user-1", username="testuser", password_hash="hash")
    db.add(user)
    db.flush()
    portfolio = Portfolio(id="port-1", user_id=user.id, name="Main", currency="EUR")
    db.add(portfolio)
    db.commit()
    return user, portfolio


def _holding(portfolio_id, isin, asset_type, source="manual", name="Holding"):
    return Holding(
        portfolio_id=portfolio_id,
        isin=isin,
        name=name,
        asset_type=asset_type,
        quantity=Decimal("10"),
        avg_buy_price=Decimal("80.00"),
        currency="EUR",
        source=source,
    )


def _seed_user_with_depot(db):
    user = User(id="user-dkb", username="dkbuser", password_hash="hash")
    db.add(user)
    db.flush()
    account = DkbAccount(
        user_id=user.id, type="depot", iban="DE02120300000000202051", balance=Decimal("0"),
    )
    db.add(account)
    db.flush()
    return user, account


def _snapshot(account, positions):
    return {
        "accounts": [
            {"type": account.type, "iban": account.iban, "balance": Decimal("0"), "currency": "EUR"},
        ],
        "transactions": [],
        "positions": positions,
    }


def _position(account, isin=ETF_ISIN):
    return {
        "iban": account.iban,
        "isin": isin,
        "name": "iShares Core MSCI World UCITS ETF USD (Acc)",
        "quantity": Decimal("10"),
        "current_price": Decimal("80.00"),
        "current_value": Decimal("800.00"),
        "avg_buy_price": Decimal("75.00"),
    }


# ---------------------------------------------------------------------------
# 1. classify_and_enrich repairs stale holding types from Asset truth
# ---------------------------------------------------------------------------


def test_classify_and_enrich_repairs_stale_holding_asset_type():
    """Given an Asset typed 'etf' and a dkb_sync holding still typed 'stock',
    When classify_and_enrich runs fully offline, Then the holding inherits the
    Asset's authoritative type even though this run's lookups all failed."""
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    db.add(Asset(
        isin=ETF_ISIN, symbol="EUNL.DE", exchange="XETRA",
        name="iShares Core MSCI World UCITS ETF", asset_type="etf", currency="EUR",
    ))
    db.add(_holding(portfolio.id, ETF_ISIN, "stock", source="dkb_sync"))
    db.commit()

    result = classify_and_enrich(db, user.id)

    holding = db.query(Holding).filter(Holding.isin == ETF_ISIN).one()
    assert holding.asset_type == "etf"
    assert result["errors"]  # offline guard fired; repair is independent of this run's lookups


# ---------------------------------------------------------------------------
# 2. Manual holdings without an ISIN are never touched
# ---------------------------------------------------------------------------


def test_manual_holding_without_isin_untouched_while_isin_sibling_repaired():
    """Given a manual holding WITHOUT an ISIN next to a drifted dkb_sync
    holding, When classify_and_enrich runs, Then only the ISIN-matching row is
    re-typed and the no-ISIN row keeps its hand-entered typing."""
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    db.add(Asset(
        isin=ETF_ISIN, symbol="EUNL.DE", exchange="XETRA",
        name="iShares Core MSCI World UCITS ETF", asset_type="etf", currency="EUR",
    ))
    db.add(_holding(portfolio.id, ETF_ISIN, "stock", source="dkb_sync"))
    orphan = _holding(portfolio.id, None, "stock", name="Hand-entered stock")
    db.add(orphan)
    db.commit()

    classify_and_enrich(db, user.id)

    assert orphan.asset_type == "stock"
    repaired = db.query(Holding).filter(Holding.isin == ETF_ISIN).one()
    assert repaired.asset_type == "etf"


# ---------------------------------------------------------------------------
# 3. Sync UPDATE path re-types instead of preserving stale types
# ---------------------------------------------------------------------------


def test_sync_update_path_retypes_existing_holding_without_recreating_row():
    """Given a synced holding typed from its Asset, When the Asset's type flips
    ('etf' → 'stock') and the next sync runs, Then the SAME row is re-typed —
    not deleted and recreated."""
    db = _memory_db()
    user, account = _seed_user_with_depot(db)
    db.add(Asset(
        isin=ETF_ISIN, symbol="IWDA", exchange="XETRA",
        name="iShares Core MSCI World UCITS ETF USD (Acc)", asset_type="etf", currency="EUR",
    ))
    db.commit()

    DkbSyncService().persist_snapshot(db, user.id, _snapshot(account, [_position(account)]))
    sync_dkb_positions_to_holdings(db, user.id)
    holding = db.query(Holding).filter(Holding.source == "dkb_sync").one()
    assert holding.asset_type == "etf"
    original_id = holding.id

    asset = db.query(Asset).filter(Asset.isin == ETF_ISIN).one()
    asset.asset_type = "stock"
    db.commit()

    DkbSyncService().persist_snapshot(db, user.id, _snapshot(account, [_position(account)]))
    sync_dkb_positions_to_holdings(db, user.id)

    after = db.query(Holding).filter(Holding.source == "dkb_sync").one()
    assert after.id == original_id
    assert after.asset_type == "stock"


# ---------------------------------------------------------------------------
# 4. propagate_asset_types: per-ISIN counts, idempotence, unknowns untouched
# ---------------------------------------------------------------------------


def test_propagate_returns_changed_counts_per_isin():
    """Given drifted holdings across both sources, When propagate runs, Then it
    reports exactly one changed row per ISIN."""
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    db.add(Asset(
        isin=ETF_ISIN, symbol="EUNL.DE", exchange="XETRA",
        name="World ETF", asset_type="etf", currency="EUR",
    ))
    db.add(Asset(
        isin=STOCK_ISIN, symbol="AAPL", exchange="NMS",
        name="Apple Inc.", asset_type="stock", currency="USD",
    ))
    db.add(_holding(portfolio.id, ETF_ISIN, "stock", source="dkb_sync"))
    db.add(_holding(portfolio.id, STOCK_ISIN, "etf", source="manual"))
    db.commit()

    counts = propagate_asset_types(db, [ETF_ISIN, STOCK_ISIN])

    assert counts == {ETF_ISIN: 1, STOCK_ISIN: 1}


def test_propagate_second_call_is_noop_and_unknowns_untouched():
    """Given already-correct rows and an ISIN with no Asset evidence, When
    propagate runs twice, Then both calls change nothing and the no-evidence
    row keeps its typing."""
    db = _memory_db()
    user, portfolio = _seed_user_and_portfolio(db)
    db.add(Asset(
        isin=ETF_ISIN, symbol="EUNL.DE", exchange="XETRA",
        name="World ETF", asset_type="etf", currency="EUR",
    ))
    db.add(_holding(portfolio.id, ETF_ISIN, "etf", source="dkb_sync"))
    unknown = _holding(portfolio.id, STOCK_ISIN, "stock", source="manual")  # no Asset row
    db.add(unknown)
    db.commit()

    first = propagate_asset_types(db, [ETF_ISIN, STOCK_ISIN])
    second = propagate_asset_types(db, [ETF_ISIN, STOCK_ISIN])

    assert first == {ETF_ISIN: 0, STOCK_ISIN: 0}
    assert second == {ETF_ISIN: 0, STOCK_ISIN: 0}
    assert unknown.asset_type == "stock"
