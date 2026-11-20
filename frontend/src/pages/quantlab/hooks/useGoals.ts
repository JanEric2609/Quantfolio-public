import { useQuery } from "@tanstack/react-query";
import { api, Goal } from "../../../lib/api";

export const useGoals = () =>
  useQuery({
    queryKey: ["quant", "goals"],
    queryFn: () => api<Goal[]>("/api/quant/goals"),
  });
