import { useQuery } from "@tanstack/react-query";
import { api, type MarketHistoryPoint } from "../lib/api";

export type PriceMove = {
  latest?: number;
  previous?: number;
  delta?: number;
  deltaPct?: number;
};

export function usePriceMoves(tickers: (string | undefined)[]) {
  const symbols = [...new Set(tickers.filter((ticker): ticker is string => Boolean(ticker)))].sort();
  return useQuery({
    queryKey: ["market-price-moves", symbols],
    queryFn: async () => {
      const rows = await Promise.all(symbols.map(async (symbol) => {
        const history = await api<MarketHistoryPoint[]>(`/api/market/history/${symbol}?days=7`);
        const latest = history.at(-1)?.close;
        const previous = history.at(-2)?.close;
        const delta = latest != null && previous != null ? latest - previous : undefined;
        return [symbol, {
          latest,
          previous,
          delta,
          deltaPct: delta != null && previous ? delta / previous : undefined,
        }] as const;
      }));
      return Object.fromEntries(rows) as Record<string, PriceMove>;
    },
    enabled: symbols.length > 0,
    staleTime: 60000,
  });
}
