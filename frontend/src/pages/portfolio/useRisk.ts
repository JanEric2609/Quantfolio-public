import { useQuery } from "@tanstack/react-query";
import { api, type RiskAssessment, type VerificationStressResult } from "../../lib/api";

/** "main" is the signed-in user's main portfolio: no separate id lookup needed. */
export function useRiskAssessment() {
  return useQuery({
    queryKey: ["portfolio-risk", "assessment"],
    queryFn: () => api<RiskAssessment>("/api/verification/risk/main"),
    staleTime: 60_000,
  });
}

export function useStressTest() {
  return useQuery({
    queryKey: ["portfolio-risk", "stress"],
    queryFn: () => api<VerificationStressResult>("/api/verification/stress/main"),
    staleTime: 60_000,
  });
}
