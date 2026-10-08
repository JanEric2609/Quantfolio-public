"""Regression tests for mt940 statement parsing in the DKB FinTS adapter.

python-fints returns mt940 Transaction objects whose fields live in the
``.data`` dict, not as attributes. The old getattr-based parsing yielded
amount 0 / empty reference / today's date for every real transaction.
"""

from datetime import date
from decimal import Decimal

from app.foundation.dkb.adapter import _parse_statement, _statement_field


class FakeAmount:
    """Mimics mt940.models.Amount."""

    def __init__(self, amount: str, currency: str = "EUR"):
        self.amount = Decimal(amount)
        self.currency = currency


class FakeMt940Transaction:
    """Mimics mt940.models.Transaction: everything lives in .data."""

    def __init__(self, data: dict):
        self.data = data


def test_statement_field_reads_from_data_dict():
    tx = FakeMt940Transaction({"purpose": "REWE sagt danke", "amount": FakeAmount("-42.50")})
    assert _statement_field(tx, "purpose") == "REWE sagt danke"
    assert _statement_field(tx, "missing", "fallback") == "fallback"


def test_statement_field_falls_back_to_attributes():
    class AttrStatement:
        purpose = "attr purpose"

    assert _statement_field(AttrStatement(), "purpose") == "attr purpose"


def test_parse_statement_extracts_real_mt940_fields():
    tx = FakeMt940Transaction(
        {
            "amount": FakeAmount("-42.50"),
            "purpose": "REWE sagt danke",
            "applicant_name": "REWE Markt GmbH",
            "posting_text": "Kartenzahlung",
            "date": date(2026, 6, 3),
        }
    )

    parsed = _parse_statement(tx, "DE00120300000000001234")

    assert parsed["amount"] == Decimal("-42.50")
    assert parsed["currency"] == "EUR"
    assert parsed["reference"] == "REWE sagt danke"
    assert parsed["date"] == date(2026, 6, 3)
    assert parsed["iban"] == "DE00120300000000001234"


def test_parse_statement_reference_fallback_chain():
    tx = FakeMt940Transaction(
        {
            "amount": FakeAmount("1000.00"),
            "purpose": "",
            "applicant_name": "Arbeitgeber AG",
            "date": date(2026, 6, 1),
        }
    )
    assert _parse_statement(tx, None)["reference"] == "Arbeitgeber AG"


def test_parse_statement_preserves_foreign_currency():
    tx = FakeMt940Transaction(
        {
            "amount": FakeAmount("-19.99", currency="USD"),
            "purpose": "AWS",
            "date": date(2026, 6, 2),
        }
    )
    assert _parse_statement(tx, None)["currency"] == "USD"


def test_parse_statement_empty_data_degrades_to_zero():
    parsed = _parse_statement(FakeMt940Transaction({}), None)
    assert parsed["amount"] == Decimal("0")
    assert parsed["reference"] == ""
