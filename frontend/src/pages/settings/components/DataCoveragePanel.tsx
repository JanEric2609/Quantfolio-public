import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getDataCoverage, listSymbols, type DataCoverage } from "../../../lib/api";
import { Input } from "../../../components/ui/input";
import { Button } from "../../../components/ui/button";

export function DataCoveragePanel() {
  const [selectedSymbol, setSelectedSymbol] = useState<string>("");
  const [searchQuery, setSearchQuery] = useState<string>("");

  const symbols = useQuery({
    queryKey: ["data-symbols"],
    queryFn: listSymbols,
    retry: 1,
  });

  const coverage = useQuery({
    queryKey: ["data-coverage", selectedSymbol],
    queryFn: () => getDataCoverage(selectedSymbol),
    enabled: !!selectedSymbol,
    retry: 1,
  });

  const filteredSymbols = (symbols.data || []).filter((s) =>
    s.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const formatDate = (dateString: string | null): string => {
    if (!dateString) return "—";
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    const diffHours = Math.floor((diffMs % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));

    if (diffDays > 0) return `${diffDays}d ago`;
    if (diffHours > 0) return `${diffHours}h ago`;
    return "just now";
  };

  return (
    <div className="space-y-4 rounded-lg border border-border bg-surface p-4">

      <div className="space-y-2">
        <Input
          placeholder="Search symbols..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="h-8"
        />
        <div className="flex flex-wrap gap-1 rounded border border-border bg-surface-2 p-2">
          {filteredSymbols.slice(0, 50).map((symbol) => (
            <Button
              key={symbol}
              variant={selectedSymbol === symbol ? "default" : "outline"}
              size="sm"
              onClick={() => setSelectedSymbol(symbol)}
              className="h-7 px-2 text-xs"
            >
              {symbol}
            </Button>
          ))}
        </div>
      </div>

      {selectedSymbol && coverage.data && (
        <div className="rounded bg-surface-2 p-3">
          <div className="grid gap-2 text-sm">
            <div className="flex justify-between">
              <span className="text-text-muted">Symbol:</span>
              <span className="font-mono font-medium">{coverage.data.symbol}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">First data:</span>
              <span>{formatDate(coverage.data.first_ts)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Latest:</span>
              <span>{formatDate(coverage.data.last_ts)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Bars:</span>
              <span>{coverage.data.bar_count}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Provider:</span>
              <span className="font-mono text-xs">{coverage.data.last_provider}</span>
            </div>
            {coverage.data.gaps_count >= 0 && (
              <div className="flex justify-between">
                <span className="text-text-muted">Business day gaps:</span>
                <span className={coverage.data.gaps_count > 5 ? "text-amber-600 dark:text-amber-400" : ""}>
                  {coverage.data.gaps_count}
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {symbols.isLoading && <p className="text-sm text-text-muted">Loading symbols…</p>}
      {symbols.error && (
        <p className="text-sm text-red-600 dark:text-red-400">Error loading data coverage: {String(symbols.error)}</p>
      )}
      {coverage.error && (
        <p className="text-sm text-red-600 dark:text-red-400">Error loading coverage: {String(coverage.error)}</p>
      )}
    </div>
  );
}
