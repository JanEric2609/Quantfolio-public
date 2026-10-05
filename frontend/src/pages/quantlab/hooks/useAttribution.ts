// useAttribution hook for attribution API
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import type { AttributionResult } from "../../../lib/api";

export interface AttributionRequest {
  portfolio_id: string;
  benchmark_ticker: string;
  date_from: string;
  date_to: string;
}

export interface BrinsonAttributionResponse {
  attribution: AttributionResult;
}

export interface AttributionRunEntry {
  id: string;
  kind: string;
  portfolio_id: string;
  benchmark: string;
  date_from: string;
  date_to: string;
  created_at: string;
  result: AttributionResult;
}

export interface AttributionRunsResponse {
  research: { runs: AttributionRunEntry[] };
}

export const useBrinsonAttribution = () => {
  return useMutation({
    mutationFn: (request: AttributionRequest) =>
      api<BrinsonAttributionResponse>("/api/attribution/brinson", {
        method: "POST",
        body: JSON.stringify(request),
      }),
  });
};

export const useFactorAttribution = () => {
  return useMutation({
    mutationFn: (request: AttributionRequest & { factors?: string[] }) =>
      api("/api/attribution/factor", {
        method: "POST",
        body: JSON.stringify(request),
      }),
  });
};

export const useAttributionRun = (runId: string | null) => {
  return useQuery({
    queryKey: ["attribution", runId],
    queryFn: () => api(`/api/attribution/runs/${runId}`),
    enabled: !!runId,
  });
};

export const useAttributionRuns = (limit = 20, offset = 0) => {
  return useQuery({
    queryKey: ["attribution-runs", limit, offset],
    queryFn: () =>
      api<AttributionRunsResponse>(`/api/attribution/runs?limit=${limit}&offset=${offset}`),
  });
};
