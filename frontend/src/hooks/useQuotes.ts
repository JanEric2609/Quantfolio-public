import { useQuery } from "@tanstack/react-query";
import { api, type MarketQuote } from "../lib/api";
import { MARKET_REQUEST_LIMIT, mapLimit } from "../lib/mapLimit";

export function useQuotes(tickers: (string | undefined)[]) {
  const symbols = [...new Set(tickers.filter((ticker): ticker is string => Boolean(ticker)))].sort();
  return useQuery({
    queryKey: ["market-quotes", symbols],
    queryFn: async () => {
      const rows = await mapLimit(symbols, MARKET_REQUEST_LIMIT, (symbol) => api<MarketQuote>(`/api/market/quote/${symbol}`));
      return Object.fromEntries(rows.map((row) => [row.ticker, row])) as Record<string, MarketQuote>;
    },
    enabled: symbols.length > 0,
    staleTime: 60000,
  });
}
