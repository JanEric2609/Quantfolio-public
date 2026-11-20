import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import type { QuantOptimisation } from "../../../lib/api";

/** Covariance-only target allocations of the instruments held; ``maxWeight`` is a fraction. */
export const useQuantOptimisation = (maxWeight?: number) =>
  useQuery({
    queryKey: ["quant", "portfolio", "optimization", maxWeight ?? null],
    queryFn: () =>
      api<QuantOptimisation>(
        `/api/quant/portfolio/optimization${maxWeight ? `?max_weight=${maxWeight}` : ""}`,
      ),
  });
