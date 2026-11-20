"""Substitution agent: suggests category substitutions to reduce spending."""


from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.llm.router import call as llm_call


class Substitution(BaseModel):
    """A category substitution suggestion."""
    from_category: str
    to_category: str
    expected_savings: float | None = None
    reason: str


class SubstitutionAgentResponse(BaseModel):
    """Substitution agent response."""
    suggestions: list[Substitution]
    overall_potential_savings: float | None = None


class SubstitutionAgent:
    """Graph-based substitution suggestions over expense categories."""

    # Common substitution graph: category -> [cheaper alternatives]
    SUBSTITUTION_GRAPH = {
        "restaurants": ["meal_prep", "groceries", "home_cooking"],
        "groceries": ["bulk_buying", "seasonal_produce"],
        "subscriptions": ["free_alternatives", "shared_accounts"],
        "transport": ["public_transit", "carpooling", "cycling"],
        "entertainment": ["free_events", "hobbies"],
        "utilities": ["energy_efficiency", "cheaper_providers"],
    }

    def __init__(self, db: Session):
        self.db = db

    async def suggest_for_category(self, category: str, current_spend: float) -> SubstitutionAgentResponse:
        """Suggest substitutions for a spending category.

        Args:
            category: Expense category (e.g., "restaurants", "subscriptions")
            current_spend: Current monthly spend in that category (EUR)

        Returns:
            SubstitutionAgentResponse with actionable suggestions
        """
        alternatives = self.SUBSTITUTION_GRAPH.get(category, [])

        context = f"""
        User is spending €{current_spend:.2f}/month on {category}.
        Possible alternatives: {', '.join(alternatives) if alternatives else 'none'}

        Generate 2-3 practical substitution suggestions with estimated savings.
        Respond as JSON with: suggestions (array of {{from_category, to_category, expected_savings, reason}}), overall_potential_savings
        """

        messages = [
            {
                "role": "system",
                "content": "You are a budget optimization advisor. Suggest practical category substitutions to reduce spending. Respond with JSON only.",
            },
            {"role": "user", "content": context},
        ]

        completion = await llm_call(
            self.db,
            task_type="routine",
            messages=messages,
            structured_output=SubstitutionAgentResponse,
        )

        try:
            return SubstitutionAgentResponse.model_validate_json(completion.content)
        except Exception:
            # Fallback: no suggestions
            return SubstitutionAgentResponse(suggestions=[])
