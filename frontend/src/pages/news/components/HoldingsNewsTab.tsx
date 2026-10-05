import { formatDate } from "../../../lib/format";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type NewsItem, type NewsRefreshResult } from "../../../lib/api";
import { ChevronDown, ChevronUp } from "lucide-react";

interface Props {
  symbols: string[];
  onSelect: (item: NewsItem) => void;
  refreshResult?: NewsRefreshResult | null;
}

export function HoldingsNewsTab({ symbols, onSelect, refreshResult }: Props) {
  const [expanded, setExpanded] = useState(true);

  const okCount = refreshResult?.symbols_detail.filter((d) => d.status === "ok").length ?? 0;
  const failedCount = refreshResult?.symbols_detail.filter((d) => d.status === "failed").length ?? 0;
  const skippedCount = refreshResult?.symbols_detail.filter((d) => d.status === "skipped").length ?? 0;

  return (
    <div className="space-y-6">
      {symbols.length === 0 && (
        <p className="text-muted-foreground text-sm">
          No portfolio symbols resolved yet. Run a news refresh first.
        </p>
      )}

      {refreshResult && refreshResult.symbols_detail.length > 0 && (
        <div className="rounded-md border border-slate-800 bg-slate-900/50 p-4">
          <button
            className="flex w-full items-center justify-between"
            onClick={() => setExpanded((v) => !v)}
          >
            <div className="flex items-center gap-3 text-sm">
              <span className="font-medium">Refresh Status</span>
              <span className="inline-flex items-center rounded-full bg-success/10 px-2 py-0.5 text-xs text-success">
                {okCount} succeeded
              </span>
              {failedCount > 0 && (
                <span className="inline-flex items-center rounded-full bg-danger/10 px-2 py-0.5 text-xs text-danger">
                  {failedCount} failed
                </span>
              )}
              {skippedCount > 0 && (
                <span className="inline-flex items-center rounded-full bg-warn/10 px-2 py-0.5 text-xs text-warn">
                  {skippedCount} skipped
                </span>
              )}
            </div>
            {expanded ? (
              <ChevronUp className="h-4 w-4 text-slate-400" />
            ) : (
              <ChevronDown className="h-4 w-4 text-slate-400" />
            )}
          </button>

          {expanded && (
            <ul className="mt-3 space-y-1">
              {refreshResult.symbols_detail.map((detail) => (
                <li
                  key={detail.symbol}
                  className="flex items-center justify-between rounded-sm px-2 py-1 text-sm hover:bg-slate-800/50"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{detail.symbol}</span>
                    <StatusBadge status={detail.status} />
                    {detail.provider && detail.provider !== "unknown" && (
                      <span className="text-xs text-slate-500">{detail.provider}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-2 text-xs text-slate-400">
                    {detail.items > 0 && <span>{detail.items} items</span>}
                    {detail.message && <span className="text-slate-500">{detail.message}</span>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {symbols.map((sym) => {
        const detail = refreshResult?.symbols_detail.find((d) => d.symbol === sym);
        return (
          <HoldingNewsSection
            key={sym}
            ticker={sym}
            onSelect={onSelect}
            detail={detail}
          />
        );
      })}
    </div>
  );
}

function StatusBadge({ status }: { status: "ok" | "skipped" | "failed" }) {
  if (status === "ok") {
    return (
      <span className="inline-flex items-center rounded-full bg-success/10 px-1.5 py-0.5 text-xs text-success">
        OK
      </span>
    );
  }
  if (status === "skipped") {
    return (
      <span className="inline-flex items-center rounded-full bg-warn/10 px-1.5 py-0.5 text-xs text-warn">
        Skipped
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-danger/10 px-1.5 py-0.5 text-xs text-danger">
      Failed
    </span>
  );
}

function HoldingNewsSection({
  ticker,
  onSelect,
  detail,
}: {
  ticker: string;
  onSelect: (item: NewsItem) => void;
  detail?: { status: "ok" | "skipped" | "failed"; items: number; provider: string; message: string };
}) {
  const { data = [] } = useQuery({
    queryKey: ["news", "ticker", ticker],
    queryFn: () => api<NewsItem[]>(`/api/news?ticker=${encodeURIComponent(ticker)}`),
  });

  const hasFailed = detail?.status === "failed";
  const showSection = data.length > 0 || hasFailed;

  if (!showSection) return null;

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <h3 className="text-sm font-semibold">{ticker}</h3>
        {detail && <StatusBadge status={detail.status} />}
        {detail && detail.provider !== "unknown" && detail.provider !== "rss" && (
          <span className="text-xs text-slate-500">{detail.provider}</span>
        )}
        {detail?.provider === "rss" && (
          <span className="text-xs text-slate-500">RSS</span>
        )}
      </div>
      {data.length === 0 && hasFailed && (
        <p className="text-xs text-slate-500">
          {detail?.message || "No news available for this symbol."}
        </p>
      )}
      {data.length > 0 && (
        <ul className="space-y-1">
          {data.slice(0, 5).map((item) => (
            <li key={item.id}>
              <button
                className="text-left text-sm hover:underline text-foreground"
                onClick={() => onSelect(item)}
              >
                {item.title}
              </button>
              <span className="ml-2 text-xs text-muted-foreground">
                {formatDate(item.published_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
