"""Telegram reply formatters (Money Phase 3 §1.6/§1.7).

Pure reply-text builders for the Telegram command surface. They live above
both ``envelope``/``budget`` (their data sources) and ``telegram_bot`` (the
transport adapter) so the channel adapter stays a leaf: sending/receiving
only, no domain imports.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.foundation.budget import monthly_summary
from app.foundation.envelope import envelope_status


def format_balance_reply(db: Session, user_id: str) -> str:
    today = date.today()
    rows = envelope_status(db, user_id, today.year, today.month)
    if not rows:
        return "No categories set up yet. Add one in the Money tab first."
    lines = [f"Envelope status for {today.strftime('%B %Y')}:"]
    for row in rows:
        flag = " ⚠️" if row["overspent"] else ""
        lines.append(f"- {row['category_name']}: €{row['remaining']:.2f} left{flag}")
    return "\n".join(lines)


def format_summary_reply(db: Session, user_id: str) -> str:
    today = date.today()
    summary = monthly_summary(db, user_id, today.year, today.month)
    return (
        f"Summary for {today.strftime('%B %Y')}:\n"
        f"Spent: €{summary['spent']:.2f}\n"
        f"Income: €{summary['income']:.2f}\n"
        f"Net: €{summary['net_income']:.2f}"
    )


_THESIS_MAX_CHARS = 240


def format_mandate_review_digest(
    mandate: str,
    decision: dict,
    gate_result: dict | None,
    trade_created: bool,
    status: str,
) -> str:
    """Format a short post-review digest for a mandate review (llm_portfolio).

    Pure formatter — no DB/network access — so the caller (``review.py``)
    controls whether/when a linked Telegram account actually receives it.
    """
    action = str(decision.get("action", "hold") or "hold").upper()
    ticker = str(decision.get("ticker") or "").strip() or "—"
    thesis = str(decision.get("thesis", "") or "").strip()
    if len(thesis) > _THESIS_MAX_CHARS:
        thesis = thesis[: _THESIS_MAX_CHARS - 3] + "..."

    if action == "HOLD":
        action_line = "Action: HOLD (no trade)"
    elif trade_created:
        action_line = f"Action: {action} {ticker}"
    else:
        action_line = f"Action: {action} {ticker} (not executed)"

    if gate_result is None:
        gates_line = "Gates: n/a"
    else:
        gates_line = f"Gates: {'passed' if gate_result.get('passed') else 'failed'}"
        if not gate_result.get("passed") and gate_result.get("reason"):
            gates_line += f" — {gate_result['reason']}"

    lines = [
        f"Mandate {mandate} review complete ({status})",
        action_line,
        gates_line,
    ]
    if thesis:
        lines.append(f"Thesis: {thesis}")
    return "\n".join(lines)
