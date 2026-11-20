import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";

export type GoalRebalanceResponse = {
  available: boolean;
  current_allocation: Record<string, number>;
  target_allocation: Record<string, number>;
  bucket_weights: Record<string, number>;
  goal_buckets: {
    short_term: Array<{ title: string; years_left: number }>;
    medium_term: Array<{ title: string; years_left: number }>;
    long_term: Array<{ title: string; years_left: number }>;
  };
  suggestions: Array<{
    asset_class: string;
    current: number;
    target: number;
    action: string;
    drift_pct: number;
  }>;
  n_goals: number;
};

export function useGoalRebalance() {
  return useQuery<GoalRebalanceResponse>({
    queryKey: ["goalRebalance"],
    queryFn: () => api("/api/quant/goals/rebalance"),
  });
}
