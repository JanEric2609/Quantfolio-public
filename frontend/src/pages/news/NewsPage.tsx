import { formatDateTime } from "../../lib/format";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { api, type NewsItem, type NewsRefreshResult, type RssFeedResponse } from "../../lib/api";
import { PageHeader } from "../../components/composed/PageHeader";
import { KpiTile } from "../../components/composed/KpiTile";
import { NewsFilters } from "./components/NewsFilters";
import { NewsGrid } from "./components/NewsGrid";
import { NewsDetailSheet } from "./components/NewsDetailSheet";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { Tabs, TabsContent } from "../../components/ui/tabs";
import { TabNav } from "../../components/composed/TabNav";
import { HoldingsNewsTab } from "./components/HoldingsNewsTab";
import { FeedManager } from "./components/FeedManager";
import { RefreshCw, Rss } from "lucide-react";

// Macro is a filter on the scored news, not a tab of its own; the filters sit
// inside the one tab they apply to.
const NEWS_TABS = [
  { param: "all", label: "Scored news" },
  { param: "holdings", label: "My symbols" },
  { param: "rss", label: "RSS headlines" },
  { param: "feeds", label: "Feeds" },
];

export function NewsPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<NewsItem | null>(null);
  const [refreshResult, setRefreshResult] = useState<NewsRefreshResult | null>(null);

  // ?tab= rather than state, so a tab survives a reload and can be linked to.
  const rawTab = searchParams.get("tab") ?? "all";
  const tab = rawTab === "macro" ? "all" : rawTab;
  const ticker = searchParams.get("ticker")?.trim().toUpperCase() || undefined;
  const sentiment = searchParams.get("sentiment") || undefined;
  const source = searchParams.get("source") || undefined;
  const macroOnly = searchParams.get("macro") === "true" || rawTab === "macro";
  const relevance = searchParams.get("relevance") || undefined;

  const params = new URLSearchParams();
  if (ticker) params.set("ticker", ticker);
  if (sentiment) params.set("sentiment", sentiment);
  if (source) params.set("source", source);
  if (macroOnly) params.set("macro", "true");
  if (relevance) params.set("relevance", relevance);
  const qs = params.toString();

  const news = useQuery({
    queryKey: ["news", qs],
    queryFn: () => api<NewsItem[]>(`/api/news${qs ? `?${qs}` : ""}`),
  });
  const status = useQuery({
    queryKey: ["news-status"],
    queryFn: () => api<any>("/api/news/status"),
  });
  const rss = useQuery({
    queryKey: ["news-rss"],
    queryFn: () => api<RssFeedResponse>("/api/news/rss"),
  });
  const refresh = useMutation({
    mutationFn: () => api<NewsRefreshResult>("/api/news/refresh", { method: "POST" }),
    onSuccess: (data) => {
      setRefreshResult(data);
      queryClient.invalidateQueries({ queryKey: ["news"] });
      queryClient.invalidateQueries({ queryKey: ["news-status"] });
      const created = data?.created ?? 0;
      const symbols = data?.symbols?.length ?? 0;
      const failed = data?.failed?.length ?? 0;
      if (created > 0) {
        toast.success(`Refresh complete: ${created} new items from ${symbols} symbols`);
      } else if (failed > 0) {
        toast.warning(`Refresh complete but no new items. ${failed} symbol(s) had issues.`);
      } else {
        toast.info("Refresh complete: no new items found");
      }
    },
    onError: (error) => {
      toast.error("Refresh failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    },
  });

  return (
    <div className="space-y-6">
      <PageHeader
        title="News"
        actions={
          <div className="flex items-center gap-3">
            {refreshResult && (
              <div className="hidden sm:flex items-center gap-2 text-xs">
                <span className="inline-flex items-center rounded-full bg-success/10 px-2 py-0.5 text-success">
                  {refreshResult.symbols_detail.filter((d) => d.status === "ok").length} ok
                </span>
                {refreshResult.symbols_detail.some((d) => d.status === "failed") && (
                  <span className="inline-flex items-center rounded-full bg-danger/10 px-2 py-0.5 text-danger">
                    {refreshResult.symbols_detail.filter((d) => d.status === "failed").length} failed
                  </span>
                )}
                {refreshResult.symbols_detail.some((d) => d.status === "skipped") && (
                  <span className="inline-flex items-center rounded-full bg-warn/10 px-2 py-0.5 text-warn">
                    {refreshResult.symbols_detail.filter((d) => d.status === "skipped").length} skipped
                  </span>
                )}
              </div>
            )}
            <Button variant="accent" size="sm" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
              <RefreshCw className="h-4 w-4 mr-1" />
              {refresh.isPending ? "Refreshing..." : "Refresh"}
            </Button>
          </div>
        }
      />
      <div className="metric-grid gap-3">
        <KpiTile label="Recent Items" value={String(news.data?.length ?? 0)} />
        <KpiTile label="Macro Flags" value={String((news.data ?? []).filter((i) => i.is_macro).length)} tone="warn" />
        <KpiTile label="My symbols" value={String(status.data?.portfolio_symbols?.length ?? 0)} />
        <KpiTile
          label="Last Refresh"
          value={status.data?.latest_fetched_at ? formatDateTime(status.data.latest_fetched_at) : "Never"}
          tone={status.data?.latest_fetched_at ? "good" : "warn"}
        />
      </div>
      <Tabs value={tab}>
        <TabNav tabs={NEWS_TABS} ariaLabel="News sections" defaultParam="all" className="mb-4" />
        <TabsContent value="all" className="space-y-4">
          <NewsFilters
            ticker={ticker}
            sentiment={sentiment}
            source={source}
            macroOnly={macroOnly}
            relevance={relevance}
            availableSources={[...new Set((news.data ?? []).map((n) => n.source).filter(Boolean))]}
            onChange={(updates) => {
              const next = new URLSearchParams(searchParams);
              Object.entries(updates).forEach(([k, v]) => {
                if (v) next.set(k, v);
                else next.delete(k);
              });
              setSearchParams(next, { replace: true });
            }}
          />
          {news.isPending ? (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-64 w-full" />
              ))}
            </div>
          ) : news.isError ? (
            <div className="rounded-md bg-destructive/10 p-4 text-sm text-destructive flex items-center justify-between">
              <span>Failed to load news</span>
              <Button variant="outline" size="sm" onClick={() => news.refetch()}>Retry</Button>
            </div>
          ) : (
            <NewsGrid items={news.data ?? []} onSelect={(item) => setSelected(item)} />
          )}
        </TabsContent>
        <TabsContent value="holdings">
          <HoldingsNewsTab
            symbols={status.data?.portfolio_symbols ?? []}
            onSelect={(item) => setSelected(item)}
            refreshResult={refreshResult}
          />
        </TabsContent>
        <TabsContent value="rss">
          <RssFeedTab data={rss.data} isLoading={rss.isPending} />
        </TabsContent>
        <TabsContent value="feeds">
          <FeedManager />
        </TabsContent>
      </Tabs>
      <NewsDetailSheet
        item={selected}
        open={!!selected}
        onOpenChange={(open) => { if (!open) setSelected(null); }}
      />
    </div>
  );
}

function RssFeedTab({ data, isLoading }: { data?: RssFeedResponse; isLoading: boolean }) {
  if (isLoading) {
    return <div className="p-4 text-sm text-text-secondary">Loading RSS feeds...</div>;
  }

  if (!data) {
    return <div className="p-4 text-sm text-text-secondary">No RSS data available</div>;
  }

  if (data.errors.length > 0) {
    return (
      <div className="space-y-4">
        <div className="rounded-md bg-warn/10 p-3 text-sm text-warn">
          Some feeds had errors: {data.errors.slice(0, 3).join("; ")}
        </div>
        <RssFeedList items={data.items} />
      </div>
    );
  }

  return <RssFeedList items={data.items} />;
}

function RssFeedList({ items }: { items: RssFeedResponse["items"] }) {
  if (items.length === 0) {
    return <div className="p-4 text-sm text-text-secondary">No RSS items found</div>;
  }

  return (
    <div className="space-y-3">
      {items.map((item, i) => (
        <a
          key={i}
          href={item.link}
          target="_blank"
          rel="noopener noreferrer"
          className="block rounded-md border border-border bg-surface p-4 transition-colors hover:bg-surface-2"
        >
          <div className="flex items-start gap-3">
            <Rss className="mt-1 h-4 w-4 shrink-0 text-warn" />
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-medium text-text-primary line-clamp-2">{item.title}</h3>
              {item.summary && (
                <p className="mt-1 text-xs text-text-secondary line-clamp-2">{item.summary}</p>
              )}
              <div className="mt-2 flex items-center gap-3 text-xs text-text-muted">
                <span>{item.source}</span>
                {item.published && <span>{item.published}</span>}
              </div>
            </div>
          </div>
        </a>
      ))}
    </div>
  );
}
