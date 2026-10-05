"""Explainer agent: provides natural-language explanations of charges and anomalies."""

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.llm.router import call as llm_call


class ExplanationResponse(BaseModel):
    """Explainer agent response."""
    explanation: str
    is_unusual: bool
    recommendation: str | None = None


class ExplainerAgent:
    """Natural-language explanation of a charge or anomaly."""

    def __init__(self, db: Session):
        self.db = db

    async def explain(
        self,
        amount: float,
        category: str,
        description: str,
        date: str,
        historical_avg: float | None = None,
    ) -> ExplanationResponse:
        """Explain a specific charge.

        Args:
            amount: Charge amount (EUR)
            category: Expense category
            description: Charge description/merchant
            date: Charge date (ISO format)
            historical_avg: Historical average for this category (EUR), if available

        Returns:
            ExplanationResponse with natural-language explanation
        """
        context = f"""
        Charge details:
        - Amount: €{amount:.2f}
        - Category: {category}
        - Description: {description}
        - Date: {date}
        """

        if historical_avg is not None:
            deviation_pct = ((amount - historical_avg) / historical_avg * 100) if historical_avg > 0 else 0
            context += f"\n- Historical average for {category}: €{historical_avg:.2f}/month ({deviation_pct:+.1f}%)"

        context += "\n\nProvide a brief explanation of what this charge likely represents, whether it's unusual, and any recommendation."

        messages = [
            {
                "role": "system",
                "content": "You are a personal finance analyst. Explain charges in plain language and flag anomalies. Respond with JSON only.",
            },
            {"role": "user", "content": context},
        ]

        completion = await llm_call(
            self.db,
            task_type="routine",
            messages=messages,
            structured_output=ExplanationResponse,
        )

        try:
            return ExplanationResponse.model_validate_json(completion.content)
        except Exception:
            # Fallback explanation
            return ExplanationResponse(
                explanation=f"{description} on {date} for €{amount:.2f}",
                is_unusual=historical_avg is not None and amount > historical_avg * 1.5,
            )
