import { formatDate } from "../../../lib/format";
import { useResearchSummary } from "../hooks/useResearchSummary";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";

interface ResearchSyncResponse {
  synced: boolean;
  synced_count: number;
  reports_count: number;
  message?: string;
}

export function ResearchView() {
  const { data, isLoading, isError } = useResearchSummary();
  const sync = useMutation({
    mutationFn: () =>
      api<ResearchSyncResponse>("/api/quant/research/sync", { method: "POST" }),
  });

  if (isLoading)
    return (
      <div className="p-4 text-sm text-text-secondary">
        Loading research summary...
      </div>
    );
  if (isError)
    return (
      <div className="p-4 text-sm text-danger">
        Failed to load research summary.
      </div>
    );

  const metrics: MetricItem[] = [
    { label: "Total Reports", value: String(data?.total_reports ?? 0) },
    {
      label: "Unique Tickers",
      value: String(data?.unique_tickers?.length ?? 0),
    },
    {
      label: "Latest Report",
      value: data?.latest_report_date
        ? formatDate(data.latest_report_date)
        : "None",
      mono: false,
    },
  ];

  return (
    <div className="space-y-0">
      {/* Summary Strip */}
      <SummaryStrip metrics={metrics} />

      {/* Toolbar */}
      <div className="flex items-center justify-between border-b border-line py-3">
        <span />
        <button
          onClick={() => sync.mutate()}
          className="rounded border border-line bg-surface px-3 py-1 text-xs hover:bg-surface-2"
          disabled={sync.isPending}
        >
          {sync.isPending ? "Syncing..." : "Sync to Obsidian"}
        </button>
      </div>

      <div className="space-y-4 pt-4">
        {data?.ticker_counts &&
          Object.keys(data.ticker_counts).length > 0 && (
            <section className="rounded-md p-3 bg-surface">
              <h2 className="mb-2 text-sm font-semibold text-text-primary">
                Reports by Ticker
              </h2>
              <div className="flex flex-wrap gap-2">
                {Object.entries(data.ticker_counts as Record<string, number>)
                  .sort((a, b) => b[1] - a[1])
                  .map(([ticker, count]) => (
                    <span
                      key={ticker}
                      className="rounded px-2 py-1 text-xs"
                      style={{ background: "rgb(var(--c-surface-2))" }}
                    >
                      <span className="font-medium">{ticker}</span>{" "}
                      <span className="text-text-muted">({count})</span>
                    </span>
                  ))}
              </div>
            </section>
          )}

        {sync.isError && (
          <div className="rounded-md border border-danger/50 p-3 text-xs text-danger">
            Sync failed: {sync.error?.message ?? "Unknown error"}
          </div>
        )}

        {sync.data && (
          <div
            className={`rounded-md border p-3 text-xs ${
              sync.data.synced
                ? "border-success/50 text-success"
                : "border-warn/50 text-warn"
            }`}
          >
            {sync.data.synced
              ? `Synced ${sync.data.synced_count}/${sync.data.reports_count} reports to Obsidian.`
              : sync.data.message ?? "Sync partially completed."}
          </div>
        )}
      </div>
    </div>
  );
}
