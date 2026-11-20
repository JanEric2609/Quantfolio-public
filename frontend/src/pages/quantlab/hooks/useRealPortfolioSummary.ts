import { useQuery } from "@tanstack/react-query";
import { api, type RealPortfolioMetricsResponse } from "../../../lib/api";

export function useRealPortfolioSummary(benchmark = "", lookbackDays = 365) {
  return useQuery({
    queryKey: ["quant", "portfolio", "real", "summary", benchmark, lookbackDays],
    queryFn: () =>
      api<RealPortfolioMetricsResponse>(
        `/api/quant/portfolio/real/summary?${new URLSearchParams({ lookback_days: String(lookbackDays), ...(benchmark ? { benchmark } : {}) })}`,
      ),
    staleTime: 5 * 60_000,
  });
}
