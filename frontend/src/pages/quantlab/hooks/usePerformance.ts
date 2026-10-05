// usePerformance hook for performance ledger API
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import type { LedgerBenchmark, PerformanceLedgerEntry } from "../../../lib/api";

export interface CompositeRequest {
  name: string;
  portfolio_ids: string[];
}

export interface LedgerSnapshotRequest {
  composite_id: string;
  as_of: string;
  dispersion?: number;
  ex_post_risk?: Record<string, number>;
}

export interface CompositeItem {
  id: string;
  name: string;
}

export interface CompositesResponse {
  research: {
    composites: CompositeItem[];
  };
}

export interface LedgerSummaryData {
  twr: number;
  mwr: number;
  dispersion?: number;
  ex_post_risk?: PerformanceLedgerEntry["ex_post_risk"];
}

export interface LedgerSummaryResponse {
  research: LedgerSummaryData;
}

export interface LedgerEntryItem {
  id: string;
  as_of: string;
  twr: number;
  mwr: number;
  dispersion?: number | null;
  ex_post_risk?: Partial<PerformanceLedgerEntry["ex_post_risk"]>;
  snapshot_meta?: { snapshots_used?: number; cashflows_used?: number };
}

export interface LedgerEntriesResponse {
  research: {
    composite_id: string;
    entries: LedgerEntryItem[];
    count: number;
  };
}

export const useCreateComposite = () => {
  return useMutation({
    mutationFn: (request: CompositeRequest) =>
      api("/api/performance/composites", {
        method: "POST",
        body: JSON.stringify(request),
      }),
  });
};

export const useComposites = () => {
  return useQuery({
    queryKey: ["composites"],
    queryFn: () => api<CompositesResponse>("/api/performance/composites"),
  });
};

export const useLedgerEntries = (compositeId: string | null, limit = 50) => {
  return useQuery({
    queryKey: ["ledger-entries", compositeId, limit],
    queryFn: () =>
      api<LedgerEntriesResponse>(`/api/performance/ledger?composite_id=${compositeId}&limit=${limit}`),
    enabled: !!compositeId,
  });
};

export const useLedgerSummary = (compositeId: string | null) => {
  return useQuery({
    queryKey: ["ledger-summary", compositeId],
    queryFn: () => api<LedgerSummaryResponse>(`/api/performance/ledger/${compositeId}/summary`),
    enabled: !!compositeId,
    // The summary 404s until the first snapshot is written — that's an
    // expected empty state, not a transient failure worth retrying.
    retry: false,
  });
};

export interface LedgerBenchmarkResponse {
  research: LedgerBenchmark;
}

/** Latest ledger TWR beside the passive core ETF's return over the same window. */
export const useLedgerBenchmark = (compositeId: string | null) => {
  return useQuery({
    queryKey: ["ledger-benchmark", compositeId],
    queryFn: () => api<LedgerBenchmarkResponse>(`/api/performance/ledger/${compositeId}/benchmark`),
    enabled: !!compositeId,
    // 404 until there is a ledger entry and a portfolio snapshot: an empty state, not a failure.
    retry: false,
  });
};

export const useWriteLedgerSnapshot = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: LedgerSnapshotRequest) =>
      api("/api/performance/ledger/snapshot", {
        method: "POST",
        body: JSON.stringify(request),
      }),
    onSuccess: () => {
      // Refresh the ledger views so the new entry is visible immediately.
      void queryClient.invalidateQueries({ queryKey: ["ledger-entries"] });
      void queryClient.invalidateQueries({ queryKey: ["ledger-summary"] });
      void queryClient.invalidateQueries({ queryKey: ["ledger-benchmark"] });
    },
  });
};
