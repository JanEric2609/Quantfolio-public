"""Three-artifact envelope (Phase 3 cross-cutting pattern).

Every performance-emitting API endpoint returns a three-artifact envelope:
- research: Algorithm/computation results
- attribution: Attribution analysis (Brinson, factor, etc.)
- ledger_ref: Link to performance ledger entry
"""


def three_artifacts(
    research: dict,
    attribution: dict | None = None,
    ledger_ref: str | None = None,
) -> dict:
    """
    Package results into standardized three-artifact envelope.

    Args:
        research: Primary computation result
        attribution: Attribution analysis dict (optional)
        ledger_ref: Performance ledger entry ID (optional)

    Returns:
        {research, attribution, ledger_ref} envelope
    """
    return {
        "research": research,
        "attribution": attribution,
        "ledger_ref": ledger_ref,
    }
