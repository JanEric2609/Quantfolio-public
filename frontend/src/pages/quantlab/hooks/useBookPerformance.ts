import { useQuery } from "@tanstack/react-query";
import { api, type BookPerformance } from "../../../lib/api";

/** Time-weighted unit value of the real book (every broker, EUR) with a rebased benchmark. */
export const useBookPerformance = () =>
  useQuery({
    queryKey: ["quant", "portfolio", "real", "performance"],
    queryFn: () => api<BookPerformance>("/api/quant/portfolio/real/performance"),
    staleTime: 5 * 60_000,
  });
