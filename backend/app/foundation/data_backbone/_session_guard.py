"""Transaction isolation for raw-SQL statements that share a caller's session.

``BarStore`` / ``RegimeStore`` / ``FactorStore`` are frequently constructed with
a caller's SQLAlchemy session (e.g. the Discover pipeline passes its own ``db``
into ``market.history`` -> ``BarStore``). A raw ``SELECT``/``INSERT`` that fails
(for example ``UndefinedTable`` when a hypertable has not been migrated yet)
aborts the enclosing PostgreSQL transaction; every later statement on that
session then fails with ``InFailedSqlTransaction`` (SQLSTATE 25P02) until the
transaction is rolled back. That turns a missing/empty table into a confusing
cascade that aborts the whole caller transaction (see the Discover
``InFailedSqlTransaction`` incident).

``safe_execute`` wraps the statement in a SAVEPOINT so the failure is confined:
the savepoint is rolled back on error but the caller's outer transaction keeps
running, letting the caller degrade to its next fallback instead of dying with a
transaction-aborted error.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Executable, TextClause, text
from sqlalchemy.engine import Result
from sqlalchemy.orm import Session


def safe_execute(
    session: Session,
    statement: Executable | TextClause | str,
    params: dict[str, Any] | None = None,
) -> Result[Any]:
    """Execute *statement* on *session* inside a SAVEPOINT.

    The savepoint confines any failure (e.g. a missing hypertable) to this
    single statement so the caller's transaction is never left poisoned. The
    exception is re-raised for the caller's existing try/except to handle.
    """
    executable = text(statement) if isinstance(statement, str) else statement
    with session.begin_nested():
        return session.execute(executable, params)
