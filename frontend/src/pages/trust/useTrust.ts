import { useQuery } from "@tanstack/react-query";
import { api, type TrustCalls, type TrustHistory, type TrustRanking, type TrustTypeKey, type TrustVerdict } from "../../lib/api";

export const trustVerdictKey = ["trust", "verdict"] as const;

/** Verdict + one evidence row per prediction type (GET /api/trust/verdict). */
export function useTrustVerdict() {
  return useQuery({
    queryKey: trustVerdictKey,
    queryFn: () => api<TrustVerdict>("/api/trust/verdict"),
    staleTime: 60_000,
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
