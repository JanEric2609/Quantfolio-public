import { useQuery } from "@tanstack/react-query";
import { api, type RealRebalanceResponse } from "../../../lib/api";

/** Band rebalance toward a covariance-only target (``erc`` by default). */
export function useRealRebalance(target = "erc", lookbackDays = 365) {
  return useQuery({
    queryKey: ["quant", "portfolio", "real", "rebalance", target, lookbackDays],
    queryFn: () =>
      api<RealRebalanceResponse>(
        `/api/quant/portfolio/real/rebalance?lookback_days=${lookbackDays}&target=${encodeURIComponent(target)}`,
      ),
    staleTime: 5 * 60_000,
  });
}
