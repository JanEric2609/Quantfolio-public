import { useQuery } from "@tanstack/react-query";
import { api, type RealPortfolioRiskResponse } from "../../../lib/api";

export function useRealPortfolioRisk(benchmark = "", lookbackDays = 365) {
  return useQuery({
    queryKey: ["quant", "portfolio", "real", "risk", benchmark, lookbackDays],
    queryFn: () =>
      api<RealPortfolioRiskResponse>(
        `/api/quant/portfolio/real/risk?${new URLSearchParams({ lookback_days: String(lookbackDays), ...(benchmark ? { benchmark } : {}) })}`,
      ),
    staleTime: 5 * 60_000,
  });
}
