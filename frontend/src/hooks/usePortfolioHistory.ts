import { useQuery } from "@tanstack/react-query";
import { api, type PortfolioSnapshot } from "../lib/api";
import { type ChartRangeKey, daysForRange } from "../lib/chartRange";

/** The Dashboard's ranges are the stock chart's (one shared list, lib/chartRange.ts). */
export type PortfolioRange = ChartRangeKey;

export function usePortfolioHistory(range: PortfolioRange) {
  const days = daysForRange(range);
  return useQuery({
    queryKey: ["portfolio-snapshots", days],
    queryFn: () => api<PortfolioSnapshot[]>(`/api/portfolio/snapshots?days=${days}`),
  });
}
