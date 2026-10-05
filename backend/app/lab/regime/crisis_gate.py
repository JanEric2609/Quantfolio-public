"""Crisis gate: pure rule-based override for regime classification.

Evaluates crisis conditions based on market indicators (VIX, credit spread, drawdown).
This is a stateless, pure function with no database or I/O dependencies.
"""


def evaluate_crisis(
    *,
    vix: float | None,
    credit_spread: float | None,
    drawdown: float | None,
    vix_threshold: float,
    credit_spread_threshold: float,
    drawdown_threshold: float,
) -> dict:
    """Evaluate whether crisis conditions are met.

    Spec rule: crisis = (VIX > θ_vix) OR (credit_spread > θ_cs) OR (drawdown < θ_dd)

    A None input for any signal means that signal is unavailable and cannot
    trigger crisis (treated as not-firing).

    Boundary semantics:
    - VIX: strictly '>' (not '>=')
    - Credit spread: strictly '>' (not '>=')
    - Drawdown: strictly '<' (not '<='), since drawdown is negative

    Args:
        vix: Current VIX level (or None if unavailable).
        credit_spread: Current credit spread in percentage points (or None if unavailable).
        drawdown: Current drawdown as fraction (e.g., -0.15 for -15%, or None if unavailable).
        vix_threshold: VIX threshold above which crisis fires.
        credit_spread_threshold: Credit spread threshold above which crisis fires.
        drawdown_threshold: Drawdown threshold (negative) below which crisis fires.

    Returns:
        Dict with:
            - "crisis" (bool): True if any condition fires, False otherwise.
            - "triggers" (list[str]): Which conditions fired ("vix", "credit_spread", "drawdown").
    """
    triggers = []

    # Check VIX (strictly greater than threshold)
    if vix is not None and vix > vix_threshold:
        triggers.append("vix")

    # Check credit spread (strictly greater than threshold)
    if credit_spread is not None and credit_spread > credit_spread_threshold:
        triggers.append("credit_spread")

    # Check drawdown (strictly less than threshold, i.e., more negative)
    if drawdown is not None and drawdown < drawdown_threshold:
        triggers.append("drawdown")

    return {
        "crisis": len(triggers) > 0,
        "triggers": triggers,
    }
