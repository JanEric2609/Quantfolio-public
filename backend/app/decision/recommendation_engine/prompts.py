"""LLM prompt templates for the Investment Recommendation Engine.

These prompts constrain the LLM to produce structured JSON that can be
parsed and validated against the context bundle. The key anti-hallucination
technique: the prompt explicitly lists available data sources and tells the
LLM it may ONLY reference those sources.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.foundation.llm.untrusted import wrap_untrusted

if TYPE_CHECKING:
    from app.decision.recommendation_engine.models import ContextBundle

# The JSON schema the LLM must follow
RECOMMENDATION_JSON_SCHEMA = """
{
  "ticker": "string — ticker symbol",
  "action": "string — BUY | HOLD | SELL | AVOID",
  "confidence": "string — low | medium | high",
  "timeframe": "string — e.g. '3-6 months', '6-12 months'",
  "horizon_months": "integer — the timeframe above, expressed in months (e.g. 4)",
  "thesis": "string — one-sentence investment thesis",
  "bull_case": "string — one sentence on why this could go well, or empty if action is not BUY/HOLD",
  "bear_case": "string — one sentence on why this could go badly, always fill this in",
  "evidence": [
    {
      "source": "string — dot-notation path to data source",
      "value": "number or string — the actual value from that source",
      "interpretation": "string — what this value means"
    }
  ],
  "risks": ["string — key risk factors"]
}
"""


def build_recommendation_prompt(
    context: ContextBundle,
    weights: dict[str, float] | None = None,
) -> str:
    """Build the system prompt for recommendation generation.

    This prompt is designed to minimize hallucination by:
    1. Explicitly listing available data sources with their values
    2. Forbidding claims that can't be traced to provided data
    3. Requiring source attribution in the output
    """
    # Build the available sources inventory
    sources_section = _build_sources_inventory(context)

    # Build the portfolio summary
    portfolio_section = _build_portfolio_summary(context)

    # Build the weights emphasis section
    weights_section = _build_weights_section(weights)

    return f"""You are a quantitative investment analyst. Your task is to generate
buy/sell/hold recommendations for the portfolio positions based on the data provided.

CRITICAL RULES:
1. You may ONLY reference data that appears in the "Available Data" section below.
2. Every claim MUST include a source tag referencing where the data came from.
3. If data is missing or insufficient for a position, say "Insufficient data for recommendation."
4. Do NOT invent or estimate values that aren't provided.
5. These are estimates, not financial advice. Always include appropriate disclaimers.

{sources_section}

{portfolio_section}

{weights_section}

OUTPUT FORMAT:
Return a JSON array of recommendation objects. Each object must follow this schema:
{RECOMMENDATION_JSON_SCHEMA}

IMPORTANT:
- The "source" field in evidence must match one of the "Available Data" paths above.
- If a metric is unavailable, omit it from evidence — do not make up a value.
- Set confidence based on data completeness: "low" if <3 sources, "medium" if 3-5, "high" if >5.
- Action rationale must reference specific numbers from the provided data.

Return ONLY the JSON array. No markdown, no explanation outside the JSON."""


def _build_sources_inventory(context: ContextBundle) -> str:
    """Build the section listing all available data sources and their values."""
    lines = ["## Available Data\n"]

    # Portfolio
    lines.append("### Portfolio")
    lines.append(f"- Total value: {context.portfolio.total_value} EUR")
    lines.append(f"- Currency: {context.portfolio.currency}")
    if context.portfolio.asset_allocation:
        alloc = ", ".join(
            f"{k}: {v:.1f}%" for k, v in context.portfolio.asset_allocation.items()
        )
        lines.append(f"- Asset allocation: {alloc}")
    lines.append("")

    # Per-ticker metrics
    if context.metrics:
        lines.append("### Quantitative Metrics")
        for ticker, m in context.metrics.items():
            lines.append(f"\n#### {ticker}")
            lines.append(f"  - metrics.{ticker}.sortino = {m.sortino}")
            lines.append(f"  - metrics.{ticker}.sharpe = {m.sharpe}")
            lines.append(f"  - metrics.{ticker}.calmar = {m.calmar}")
            lines.append(f"  - metrics.{ticker}.cvar_95 = {m.cvar_95}")
            lines.append(f"  - metrics.{ticker}.max_drawdown = {m.max_drawdown}")
            lines.append(f"  - metrics.{ticker}.annualised_return = {m.annualised_return}")
            lines.append(f"  - metrics.{ticker}.annualised_volatility = {m.annualised_volatility}")
        lines.append("")

    # Per-ticker fundamentals
    if context.fundamentals:
        lines.append("### Fundamental Data")
        for ticker, f in context.fundamentals.items():
            lines.append(f"\n#### {ticker}")
            lines.append(f"  - fundamentals.{ticker}.piotroski_score = {f.piotroski_score}")
            if f.pe_ratio is not None:
                lines.append(f"  - fundamentals.{ticker}.pe_ratio = {f.pe_ratio}")
            if f.roe is not None:
                lines.append(f"  - fundamentals.{ticker}.roe = {f.roe}")
            if f.debt_equity is not None:
                lines.append(f"  - fundamentals.{ticker}.debt_equity = {f.debt_equity}")
            if f.profit_margin is not None:
                lines.append(f"  - fundamentals.{ticker}.profit_margin = {f.profit_margin}")
        lines.append("")

    # Per-ticker forward-earnings estimate signal (IBES) — ticker-matched,
    # not gvkey-verified, so the caveat travels with every value.
    if context.estimates:
        lines.append("### Analyst Estimate Data (ticker-matched, not a verified identifier link)")
        for ticker, est in context.estimates.items():
            if est.sue is None and est.revision_momentum is None and est.dispersion is None:
                continue
            lines.append(f"\n#### {ticker}")
            if est.sue is not None:
                lines.append(f"  - estimates.{ticker}.sue = {est.sue}")
            if est.revision_momentum is not None:
                lines.append(f"  - estimates.{ticker}.revision_momentum = {est.revision_momentum}")
            if est.dispersion is not None:
                lines.append(f"  - estimates.{ticker}.dispersion = {est.dispersion}")
        lines.append("")

    # Per-ticker SEC insider-trading signal — same ticker-match caveat.
    if context.insider_signal:
        lines.append("### Insider Trading Data (ticker-matched, not a verified identifier link)")
        for ticker, ins in context.insider_signal.items():
            if ins.cluster_buy_score is None and ins.net_insider_flow_usd is None:
                continue
            lines.append(f"\n#### {ticker}")
            if ins.cluster_buy_score is not None:
                lines.append(f"  - insider_signal.{ticker}.cluster_buy_score = {ins.cluster_buy_score}")
            if ins.net_insider_flow_usd is not None:
                lines.append(f"  - insider_signal.{ticker}.net_insider_flow_usd = {ins.net_insider_flow_usd}")
        lines.append("")

    # Per-ticker sentiment
    if context.sentiment:
        lines.append("### Sentiment Data")
        for ticker, s in context.sentiment.items():
            lines.append(f"\n#### {ticker}")
            lines.append(f"  - sentiment.{ticker}.finbert_score = {s.finbert_score}")
            lines.append(f"  - sentiment.{ticker}.news_count = {s.news_count}")
            if s.headlines:
                # News headlines are third-party text; fence them so a crafted
                # headline cannot steer the model (proposal P6).
                lines.append(
                    f"  - sentiment.{ticker}.headlines:\n"
                    + wrap_untrusted(f"{ticker} headlines", list(s.headlines[:3]))
                )
        lines.append("")

    # Per-ticker track record
    if context.track_record:
        lines.append("### Track Record (directional context, not a guarantee)")
        for ticker, t in context.track_record.items():
            lines.append(f"\n#### {ticker}")
            if t.advisor_composite_score is not None:
                lines.append(
                    f"  - track_record.{ticker}.advisor_composite_score = {t.advisor_composite_score} "
                    f"(advisor sleeve's overall 4-axis score, portfolio-level — not {ticker}-specific)"
                )
            if t.llm_win_rate is not None:
                lines.append(
                    f"  - track_record.{ticker}.llm_win_rate = {t.llm_win_rate} "
                    f"(fraction of past mandate-review calls on {ticker} that hit)"
                )
            if t.llm_verdict_history:
                lines.append(
                    f"  - track_record.{ticker}.llm_verdict_history = {t.llm_verdict_history}"
                )
        lines.append("")

    # Regime
    lines.append("### Macro Regime")
    lines.append(f"- regime.label = {context.regime.label}")
    lines.append(f"- regime.confidence = {context.regime.confidence}")
    if context.regime.vix is not None:
        lines.append(f"- regime.vix = {context.regime.vix}")
    lines.append("")

    # Data health
    lines.append("### Data Health")
    lines.append(f"- Available sources: {len(context.data_health.available)}")
    lines.append(f"- Failed sources: {len(context.data_health.failed)}")
    lines.append(f"- Degraded sources: {len(context.data_health.degraded)}")
    lines.append(f"- Completeness score: {context.data_health.completeness_score}")

    return "\n".join(lines)


def _build_portfolio_summary(context: ContextBundle) -> str:
    """Build a human-readable portfolio summary for the prompt."""
    lines = ["## Portfolio Summary\n"]

    if not context.portfolio.holdings:
        lines.append("No current holdings.")
    else:
        lines.append("Current positions:")
        for h in context.portfolio.holdings:
            ticker = h.get("ticker", "UNKNOWN")
            value = h.get("current_value", 0)
            weight = (value / context.portfolio.total_value * 100) if context.portfolio.total_value > 0 else 0
            lines.append(f"- {ticker}: {value:.2f} EUR ({weight:.1f}% of portfolio)")

    return "\n".join(lines)


def _build_weights_section(weights: dict[str, float] | None) -> str:
    if not weights:
        return ""
    lines = ["## Category Weights\n"]
    lines.append("The user has emphasized these analysis categories:")
    for category, weight in sorted(weights.items(), key=lambda x: -x[1]):
        emphasis = "HIGH EMPHASIS" if weight >= 0.25 else "moderate" if weight >= 0.15 else "low"
        lines.append(f"- {category}: {weight:.0%} ({emphasis})")
    lines.append(
        "\nWhen categories have HIGH EMPHASIS, give more weight to evidence "
        "from those data sources in your recommendation."
    )
    return "\n".join(lines)
