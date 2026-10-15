import { useQuery } from "@tanstack/react-query";
import { api, type TrustCalls, type TrustFunds, type TrustHistory, type TrustPlanBands, type TrustRanking, type TrustRiskBands, type TrustTypeKey, type TrustVerdict } from "../../lib/api";

export const trustVerdictKey = ["trust", "verdict"] as const;

/** Verdict + one evidence row per prediction type (GET /api/trust/verdict). */
export function useTrustVerdict() {
  return useQuery({
    queryKey: trustVerdictKey,
    queryFn: () => api<TrustVerdict>("/api/trust/verdict"),
    staleTime: 60_000,
  });
}

/** Held curated funds against their same-index peers, measured monthly (GET /api/trust/funds). */
export function useTrustFunds() {
  return useQuery({
    queryKey: ["trust", "funds"],
    queryFn: () => api<TrustFunds>("/api/trust/funds"),
    staleTime: 10 * 60_000,
  });
}

/** Resolved calls, newest first (GET /api/trust/calls). */
export function useTrustCalls(type: TrustTypeKey | null, limit: number, offset: number) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (type) params.set("type", type);
  return useQuery({
    queryKey: ["trust", "calls", type, limit, offset],
    queryFn: () => api<TrustCalls>(`/api/trust/calls?${params.toString()}`),
    staleTime: 60_000,
    placeholderData: (previous) => previous,
  });
}

/** Descriptive shadow-ledger metrics of Discover's ranking (GET /api/trust/ranking). */
export function useTrustRanking() {
  return useQuery({
    queryKey: ["trust", "ranking"],
    queryFn: () => api<TrustRanking>("/api/trust/ranking"),
    staleTime: 5 * 60_000,
  });
}

/** The historical panel: simulated, not live (GET /api/trust/history). */
export function useTrustHistory() {
  return useQuery({
    queryKey: ["trust", "history"],
    queryFn: () => api<TrustHistory>("/api/trust/history"),
    staleTime: 10 * 60_000,
  });
}

/** Risk forecasts stored before the fact and how they did (GET /api/trust/risk-bands). */
export function useTrustRiskBands() {
  return useQuery({
    queryKey: ["trust", "risk-bands"],
    queryFn: () => api<TrustRiskBands>("/api/trust/risk-bands"),
    staleTime: 10 * 60_000,
  });
}

export function useTrustPlanBands() {
  return useQuery({
    queryKey: ["trust", "plan-bands"],
    queryFn: () => api<TrustPlanBands>("/api/trust/plan-bands"),
    staleTime: 10 * 60_000,
  });
}
