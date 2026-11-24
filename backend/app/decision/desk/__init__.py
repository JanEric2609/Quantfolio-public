"""The guarded derivatives desk (ADR 0027): paper only, no order path.

Public surface (other contexts import this package root, never a submodule):
the limits, the ledger, the lifecycle and scorecard reads, the job registrars,
the Telegram registration and the news wire's stream thread (worker only).
"""
from .api_ops import (
    DeskActionError, chain_status, close_position_api, desk_status, intraday_view, journal_action, list_events,
    list_news, list_positions, submit_idea,
    take_proposal,
)
from .event_report import signals_overview
from .jobs import register_desk_monitor_job, register_desk_reconcile_job, register_news_wire_job
from .ledger import append_event, verify_chain
from .limits import LIMITS
from .newswire import start_stream_thread as start_news_stream
from .prereg import PREREG_HASH, register as register_desk
from .scoring import scorecard
from .service import is_halted, halt_info, set_halt, health, owner_close, propose
from .telegram import register_desk_telegram

__all__ = [
    "DeskActionError", "chain_status", "close_position_api", "desk_status", "list_events", "list_news",
    "list_positions", "intraday_view", "journal_action",
    "submit_idea", "take_proposal", "LIMITS", "PREREG_HASH", "append_event", "halt_info", "health", "is_halted", "owner_close", "propose",
    "register_desk", "register_desk_monitor_job", "signals_overview", "register_desk_reconcile_job", "register_desk_telegram",
    "register_news_wire_job", "scorecard", "set_halt", "start_news_stream", "verify_chain",
]
