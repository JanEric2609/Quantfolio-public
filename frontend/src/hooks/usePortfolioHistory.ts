import { useQuery } from "@tanstack/react-query";
import { api, type PortfolioSnapshot } from "../lib/api";

export type PortfolioRange = "1W" | "1M" | "3M" | "YTD" | "1Y" | "All";

function daysForRange(range: PortfolioRange) {
  if (range === "1W") return 7;
  if (range === "1M") return 31;
  if (range === "3M") return 92;
  if (range === "1Y") return 366;
  if (range === "All") return 3650;
  const now = new Date();
  const yearStart = new Date(now.getFullYear(), 0, 1);
  return Math.max(1, Math.ceil((now.getTime() - yearStart.getTime()) / 86400000) + 1);
}

export function usePortfolioHistory(range: PortfolioRange) {
  const days = daysForRange(range);
  return useQuery({
    queryKey: ["portfolio-snapshots", days],
    queryFn: () => api<PortfolioSnapshot[]>(`/api/portfolio/snapshots?days=${days}`),
  });
}
