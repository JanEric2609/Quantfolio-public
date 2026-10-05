"""Budget agent: interprets expense queries and suggests optimization."""

import json

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.budget import monthly_summary
from app.foundation.llm.router import call as llm_call


class BudgetInsight(BaseModel):
    """Budget agent response."""
    insight: str
    trend: str  # "up", "down", "stable"
    suggested_action: str | None = None
    affected_category: str | None = None


class BudgetAgent:
    """Natural-language budget queries and recommendations."""

    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.user_id = user_id

    async def query(self, user_query: str, year: int, month: int) -> BudgetInsight:
        """Answer a natural-language budget question.

        Args:
            user_query: User's question (e.g., "Why is groceries up this month?")
            year: Reference year
            month: Reference month (1-12)

        Returns:
            BudgetInsight with explanation and optional action
        """
        # Get budget summary for context
        summary = monthly_summary(self.db, self.user_id, year, month)

        # Build context for the LLM
        context = f"""
        User's monthly budget summary for {year}-{month:02d}:
        Total expenses: €{summary.get('total', 0):.2f}
        Categories and spending:
        {json.dumps(summary.get('by_category', {}), indent=2)}

        User question: {user_query}

        Provide a concise insight answering their question, identify the trend (up/down/stable),
        and suggest one action if relevant. Respond as JSON with fields: insight, trend, suggested_action, affected_category
        """

        messages = [
            {
                "role": "system",
                "content": "You are a personal finance advisor. Analyze spending patterns and provide helpful insights. Respond with JSON only.",
            },
            {"role": "user", "content": context},
        ]

        completion = await llm_call(
            self.db,
            task_type="routine",  # Budget queries are routine, accept latency
            messages=messages,
            structured_output=BudgetInsight,
            user_id=self.user_id,
        )

        try:
            return BudgetInsight.model_validate_json(completion.content)
        except Exception:
            # Fallback response
            return BudgetInsight(
                insight=f"Analysis in progress for: {user_query}",
                trend="stable",
                suggested_action="Review your budget settings",
            )
