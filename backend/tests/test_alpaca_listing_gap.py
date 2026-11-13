from datetime import date

from app.foundation.providers.alpaca_provider import cut_at_listing_gap


def _rows(*days):
    return [{"date": d, "close": 1} for d in days]


def test_series_without_a_gap_is_untouched():
    rows = _rows(date(2025, 1, 2), date(2025, 1, 3), date(2025, 2, 3))
    out, gap = cut_at_listing_gap(rows)
    assert out == rows and gap == 0


def test_series_is_cut_after_the_last_long_gap():
    rows = _rows(date(2018, 5, 1), date(2018, 5, 2), date(2021, 3, 1), date(2021, 3, 2), date(2021, 3, 3))
    out, gap = cut_at_listing_gap(rows)
    assert [r["date"] for r in out] == [date(2021, 3, 1), date(2021, 3, 2), date(2021, 3, 3)]
    assert gap > 60


def test_a_59_day_pause_is_kept():
    rows = _rows(date(2025, 1, 1), date(2025, 3, 1))
    assert cut_at_listing_gap(rows)[1] == 0
