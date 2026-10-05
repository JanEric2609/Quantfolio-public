import pytest
from pydantic import ValidationError

from app.foundation.schemas import CategoryIn


def test_category_in_allows_neither_goal_field():
    payload = CategoryIn(name="Groceries")
    assert payload.target_amount is None
    assert payload.target_date is None


def test_category_in_allows_both_goal_fields():
    payload = CategoryIn(name="Laptop Fund", target_amount="1200", target_date="2027-01-01")
    assert payload.target_amount == 1200
    assert payload.target_date.isoformat() == "2027-01-01"


def test_category_in_rejects_amount_without_date():
    with pytest.raises(ValidationError):
        CategoryIn(name="Laptop Fund", target_amount="1200")


def test_category_in_rejects_date_without_amount():
    with pytest.raises(ValidationError):
        CategoryIn(name="Laptop Fund", target_date="2027-01-01")
