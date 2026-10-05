"""DKB name lifecycle: wire-name garbage must never reach holdings or the UI.

Production receipt: a dkb_positions row literally stored
``"TERED SHS USD (ACC) O.N."`` — a truncated FinTS wire fragment of
"iShares Core MSCI World UCITS ETF USD (Acc) - Registered Shares ..." — while
the assets table held the clean canonical name for the same ISIN.

Three coordinated guards are locked here:

1. ``DkbSyncService.persist_snapshot`` resolves the display name Asset-FIRST
   before writing ``DkbPosition.name`` (raw wire string kept in the service log).
2. ``name_resolver`` trusts a real Asset name over ``pos["name"]`` and rejects
   truncated wire artifacts via a table-driven heuristic.
3. ``portfolio_service.sync_dkb_positions_to_holdings`` only overwrites a
   holding name when the current one is not a real name, so hand-corrected
   names survive future syncs.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from conftest import _memory_db

from app.foundation.models.entities import Asset, DkbAccount, DkbPosition, Holding, User
from app.foundation.dkb import DkbSyncService
from app.foundation.portfolio.name_resolver import _is_real_name, resolve_position_name
from app.foundation.portfolio_service import sync_dkb_positions_to_holdings

WIRE_GARBAGE = "TERED SHS USD (ACC) O.N."
CLEAN_NAME = "iShares Core MSCI World UCITS ETF USD (Acc)"
ISIN = "IE00B4L5Y983"


def _raise_offline(*args: object, **kwargs: object) -> None:
    raise RuntimeError("offline test guard")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch):
    """Hard-offline guards over every network seam this pipeline can touch."""
    monkeypatch.setattr("app.foundation.jobs.submit_job", lambda *a, **k: "stubbed")
    monkeypatch.setattr(
        "app.foundation.portfolio.name_resolver._resolve_name_via_yfinance", lambda query: None,
    )
    monkeypatch.setattr(
        "app.foundation.etf_classification.EtfUniverseProvider.lookup_by_isin", _raise_offline,
    )


def _seed_user_with_depot(db):
    user = User(id="user-1", username="testuser", password_hash="hash")
    db.add(user)
    db.flush()
    account = DkbAccount(user_id=user.id, type="depot", iban="DE02120300000000202051", balance=Decimal("0"))
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


def _position(account, isin=ISIN, name=WIRE_GARBAGE):
    return {
        "iban": account.iban,
        "isin": isin,
        "name": name,
        "quantity": Decimal("10"),
        "current_price": Decimal("80.00"),
        "current_value": Decimal("800.00"),
        "avg_buy_price": Decimal("75.00"),
    }


# ---------------------------------------------------------------------------
# 1. Write path: persist_snapshot resolves Asset-first
# ---------------------------------------------------------------------------


def test_persist_resolves_asset_name_before_writing_position(caplog):
    """Given an Asset with the clean name, When a sync persists a position whose
    wire name is the truncated artifact, Then the clean Asset name is stored and
    the raw wire string survives in the service log for forensics."""
    db = _memory_db()
    user, account = _seed_user_with_depot(db)
    db.add(Asset(isin=ISIN, symbol="IWDA", name=CLEAN_NAME, asset_type="etf"))
    db.commit()

    with caplog.at_level(logging.INFO, logger="app.foundation.dkb.service"):
        DkbSyncService().persist_snapshot(db, user.id, _snapshot(account, [_position(account)]))

    row = db.query(DkbPosition).one()
    assert row.name == CLEAN_NAME
    assert WIRE_GARBAGE not in row.name
    assert WIRE_GARBAGE in caplog.text  # raw preservation (no meta column; log-only)


def test_persist_keeps_real_name_when_no_asset_row():
    """Given no matching Asset row, When the wire name is genuinely human-readable,
    Then it is written unchanged."""
    db = _memory_db()
    user, account = _seed_user_with_depot(db)

    DkbSyncService().persist_snapshot(
        db, user.id,
        _snapshot(account, [_position(account, isin="US0378331005", name="Apple Inc.")]),
    )

    row = db.query(DkbPosition).one()
    assert row.name == "Apple Inc."


# ---------------------------------------------------------------------------
# 2. Resolver trust order + _is_real_name table
# ---------------------------------------------------------------------------


def test_resolver_prefers_real_asset_name_over_position_name():
    """Given a DB with a real Asset name, When the position carries a different
    (even plausible) name, Then the curated Asset name wins."""
    db = _memory_db()
    db.add(Asset(isin=ISIN, symbol="IWDA", name=CLEAN_NAME, asset_type="etf"))
    db.commit()

    resolved = resolve_position_name({"isin": ISIN, "ticker": "IWDA", "name": "MSCI World Tracker"}, db=db)

    assert resolved == CLEAN_NAME


def test_resolver_without_db_keeps_position_name():
    """Without a DB session the resolver degrades to the pure in-memory chain."""
    resolved = resolve_position_name({"isin": ISIN, "ticker": "IWDA", "name": "MSCI World Tracker"})

    assert resolved == "MSCI World Tracker"


def test_resolver_uses_yfinance_for_wire_garbage(monkeypatch):
    """Regression: the yfinance fallback block used to sit AFTER ``return name``
    inside the success branch, making it unreachable — wire garbage fell straight
    to the ticker fallback without ever consulting yfinance. Now step 3 runs,
    resolves the real name, and repairs a placeholder Asset row in passing."""
    db = _memory_db()
    asset = Asset(isin=ISIN, symbol="IWDA", name=WIRE_GARBAGE, asset_type="etf")
    db.add(asset)
    db.commit()

    queries: list[str] = []

    def fake_yf(query: str) -> str | None:
        queries.append(query)
        return CLEAN_NAME

    monkeypatch.setattr(
        "app.foundation.portfolio.name_resolver._resolve_name_via_yfinance", fake_yf,
    )

    resolved = resolve_position_name(
        {"isin": ISIN, "ticker": "IWDA", "name": WIRE_GARBAGE}, db=db
    )

    assert resolved == CLEAN_NAME
    assert queries == [ISIN]  # ISIN query succeeds first; ticker never needed
    db.refresh(asset)
    assert asset.name == CLEAN_NAME  # placeholder persisted so lookup won't repeat


@pytest.mark.parametrize(
    ("name", "ticker", "isin", "expected"),
    [
        # Legit names MUST pass.
        ("SAP SE O.N.", "SAP", "DE0007164600", True),
        ("Allianz SE", "ALV", "DE0008404005", True),
        ("XEON", "", "", True),
        ("iShares Core MSCI World UCITS ETF USD (Acc)", "IWDA", ISIN, True),
        ("Apple Inc.", "AAPL", "US0378331005", True),
        ("Vanguard FTSE All-World UCITS ETF (Dist)", "VGWL", "IE00BK5BQT80", True),
        # Wire artifacts MUST fail.
        (WIRE_GARBAGE, "IWDA", ISIN, False),
        ("REGISTERED SHS USD (Acc) O.N.", "", "", False),
        # Placeholders / identifier echoes MUST fail.
        ("", "SAP", "DE0007164600", False),
        (None, "SAP", "DE0007164600", False),
        ("SAP", "SAP", "DE0007164600", False),
        ("DE0007164600", "SAP", "DE0007164600", False),
    ],
)
def test_is_real_name_accept_reject_table(name, ticker, isin, expected):
    assert _is_real_name(name, ticker, isin) is expected


# ---------------------------------------------------------------------------
# 3. Holdings sync: hand-corrected names survive re-syncs
# ---------------------------------------------------------------------------


def test_second_sync_preserves_hand_corrected_holding_name():
    """Given a wire-garbage sync resolved Asset-first, When the user hand-corrects
    the holding name and the same garbage arrives on the next sync, Then the
    correction survives."""
    db = _memory_db()
    user, account = _seed_user_with_depot(db)
    db.add(Asset(isin=ISIN, symbol="IWDA", name=CLEAN_NAME, asset_type="etf"))
    db.commit()

    DkbSyncService().persist_snapshot(db, user.id, _snapshot(account, [_position(account)]))
    sync_dkb_positions_to_holdings(db, user.id)
    holding = db.query(Holding).one()
    assert holding.name == CLEAN_NAME

    # User hand-corrects the name in the UI.
    holding.name = "My World ETF"
    db.commit()

    DkbSyncService().persist_snapshot(db, user.id, _snapshot(account, [_position(account)]))
    second = sync_dkb_positions_to_holdings(db, user.id)
    assert second["updated"] == 1
    db.refresh(holding)
    assert holding.name == "My World ETF"
