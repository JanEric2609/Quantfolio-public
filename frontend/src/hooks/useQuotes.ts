import { useQuery } from "@tanstack/react-query";
import { api, type MarketQuote } from "../lib/api";

export function useQuotes(tickers: (string | undefined)[]) {
  const symbols = [...new Set(tickers.filter((ticker): ticker is string => Boolean(ticker)))].sort();
  return useQuery({
    queryKey: ["market-quotes", symbols],
    queryFn: async () => {
      const rows = await Promise.all(symbols.map((symbol) => api<MarketQuote>(`/api/market/quote/${symbol}`)));
      return Object.fromEntries(rows.map((row) => [row.ticker, row])) as Record<string, MarketQuote>;
    },
    enabled: symbols.length > 0,
    staleTime: 60000,
  });
}
