"""The outgoing desk message with its buttons (a leaf module: ``jobs``, ``events`` and ``telegram`` share it)."""
from __future__ import annotations

from typing import Any


class OutMsg(str):
    """A message with the inline buttons it is sent with and the journal row it belongs to (a ``str``, so
    callers that only read the text keep working)."""

    markup: dict[str, Any] | None
    journal_id: str | None

    def __new__(cls, text: str, markup: dict[str, Any] | None = None, journal_id: str | None = None) -> OutMsg:
        obj = super().__new__(cls, text)
        obj.markup, obj.journal_id = markup, journal_id
        return obj


def cfd_markup(journal: Any) -> dict[str, Any] | None:
    """The buttons for a journal row's state: Entered/Skipped while unanswered, Closed once entered."""
    if journal is None:
        return None
    pid = journal.position_id
    if journal.status == "sent":
        return {"inline_keyboard": [[{"text": "Entered", "callback_data": f"cfd:in:{pid}"},
                                     {"text": "Skipped", "callback_data": f"cfd:skip:{pid}"}]]}
    if journal.status == "entered":
        return {"inline_keyboard": [[{"text": "Closed", "callback_data": f"cfd:out:{pid}"}]]}
    return None
