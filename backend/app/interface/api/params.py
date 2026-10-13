"""Bounded request parameters shared by the routers.

Each bound refuses only input that used to fail with a 500 (found by the
authenticated API scan, tests/test_api_security.py):

- a year outside 1..9999 reached ``date(year, ...)``; a month outside 1..12
  did the same;
- Postgres refuses a negative LIMIT/OFFSET (SQLite reads it as "no limit")
  and one beyond a bigint;
- a huge look-back overflowed ``date.today() - timedelta(days=...)``.

The checks are validators, not ``ge``/``le`` constraints, so they stay out of
the OpenAPI schema: the contract gate (oasdiff) counts any new schema bound as
a breaking change, and these inputs never had a working answer to break.
"""
from __future__ import annotations

from typing import Annotated

from app.foundation.schemas.bounds import between

__all__ = [
    "between", "YearQuery", "MonthQuery", "YearField", "MonthField", "LimitQuery", "OffsetQuery", "DaysQuery",
    "ROWS_BOUND", "DAYS_BOUND",
]

MIN_YEAR = 1900
MAX_YEAR = 2200
MAX_DAYS = 36_500
#: Postgres keeps LIMIT/OFFSET in a bigint; 2.5e19 overflowed it (and SQLite's INTEGER).
MAX_ROWS = 10**9


#: Calendar fields, as query parameters or request-body fields.
YearQuery = Annotated[int, between(MIN_YEAR, MAX_YEAR)]
MonthQuery = Annotated[int, between(1, 12)]
YearField = YearQuery
MonthField = MonthQuery

#: Page size and offset.
ROWS_BOUND = between(0, MAX_ROWS)
LimitQuery = Annotated[int, ROWS_BOUND]
OffsetQuery = Annotated[int, ROWS_BOUND]

#: Look-back windows in days.
DAYS_BOUND = between(0, MAX_DAYS)
DaysQuery = Annotated[int, DAYS_BOUND]

# With an explicit Query(...), write ``Annotated[int, Query(...), ROWS_BOUND]``:
# nesting ``Annotated[OffsetQuery, Query(...)]`` puts the validator before the
# Query and FastAPI then renders "ge" into the schema instead of "minimum".
