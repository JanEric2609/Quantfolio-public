import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";

export type GoalContributionResponse = {
  recommended_monthly: number;
  per_goal: Array<{
    goal_id: string;
    title: string;
    recommended_monthly: number;
    existing_monthly: number;
    gap: number;
    remaining: number;
    months_left: number;
    allocation: Record<string, number>;
    risk_tolerance: string;
  }>;
  total_goals: number;
  available_savings: number;
  emergency_reserve: number;
  diagnostics: { method: string; emergency_fund_months: number };
};

export function useGoalContribution(
  monthlyIncome?: number,
  monthlyExpenses?: number,
) {
  return useQuery<GoalContributionResponse>({
    queryKey: ["goalContribution", monthlyIncome, monthlyExpenses],
    queryFn: () => {
      const params = new URLSearchParams();
      if (monthlyIncome != null) params.set("monthly_income", String(monthlyIncome));
      if (monthlyExpenses != null) params.set("monthly_expenses", String(monthlyExpenses));
      const qs = params.toString();
      return api(`/api/quant/goals/contribution${qs ? `?${qs}` : ""}`);
    },
  });
}
