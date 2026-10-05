import pytest
from decimal import Decimal
from app.foundation.schemas import TransactionIn, TransactionUpdate


def test_transaction_in_forbids_unknown():
    TransactionIn(holding_id="h", type="buy", date="2026-01-01", quantity="1", price="10")
    with pytest.raises(Exception):
        TransactionIn(holding_id="h", type="buy", date="2026-01-01", quantity="1", price="10", evil=1)


def test_transaction_update_partial():
    assert TransactionUpdate(price="11").model_dump(exclude_unset=True) == {"price": Decimal("11")}
