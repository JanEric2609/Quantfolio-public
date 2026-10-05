import { useQuery } from "@tanstack/react-query";
import { api, type TrustCalls, type TrustTypeKey, type TrustVerdict } from "../../lib/api";

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
