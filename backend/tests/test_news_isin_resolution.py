"""Tests for DKB ISIN→ticker resolution in news API."""
from decimal import Decimal
from unittest.mock import patch

from conftest import _memory_db


def test_dkb_position_isin_resolved_to_ticker():
    import uuid

    from app.foundation.models.entities import DkbAccount, DkbPosition, User
    from app.foundation.news import portfolio_symbols as _portfolio_symbols

    db = _memory_db()
    user = User(id=str(uuid.uuid4()), username="t@t.com", password_hash="x")
    db.add(user)
    acct = DkbAccount(id=str(uuid.uuid4()), user_id=user.id, type="depot", iban="DE0012345678901234567890")
    db.add(acct)
    pos = DkbPosition(id=str(uuid.uuid4()), account_id=acct.id, isin="IE00B4L5Y983", name="iShares MSCI World", quantity=Decimal("10"))
    db.add(pos)
    db.commit()

    with patch("app.foundation.news._resolve_isin_to_ticker", return_value="IWDA.AS"):
        symbols = _portfolio_symbols(db, user.id, resolve_isins=True)
    assert "IWDA.AS" in symbols


def test_dkb_position_with_existing_ticker_skips_resolution():
    import uuid

    from app.foundation.models.entities import DkbAccount, DkbPosition, User
    from app.foundation.news import portfolio_symbols as _portfolio_symbols

    db = _memory_db()
    user = User(id=str(uuid.uuid4()), username="t@t.com", password_hash="x")
    db.add(user)
    acct = DkbAccount(id=str(uuid.uuid4()), user_id=user.id, type="depot", iban="DE0012345678901234567890")
    db.add(acct)
    pos = DkbPosition(id=str(uuid.uuid4()), account_id=acct.id, isin="IE00B4L5Y983", name="iShares MSCI World", ticker="IWDA.AS", quantity=Decimal("10"))
    db.add(pos)
    db.commit()

    symbols = _portfolio_symbols(db, user.id)
    assert "IWDA.AS" in symbols


def test_dkb_position_unresolvable_isin_omitted():
    import uuid

    from app.foundation.models.entities import DkbAccount, DkbPosition, User
    from app.foundation.news import portfolio_symbols as _portfolio_symbols

    db = _memory_db()
    user = User(id=str(uuid.uuid4()), username="t@t.com", password_hash="x")
    db.add(user)
    acct = DkbAccount(id=str(uuid.uuid4()), user_id=user.id, type="depot", iban="DE0012345678901234567890")
    db.add(acct)
    pos = DkbPosition(id=str(uuid.uuid4()), account_id=acct.id, isin="XX0000000000", name="Unknown", quantity=Decimal("10"))
    db.add(pos)
    db.commit()

    with patch("app.foundation.news._resolve_isin_to_ticker", return_value=None):
        symbols = _portfolio_symbols(db, user.id, resolve_isins=True)
    assert len(symbols) == 0
