import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api, type PortfolioActivity, type WealthSummary } from "../lib/api";
import { formatCurrency, formatDate, formatSignedDelta } from "../lib/format";
import { PageHeader } from "../components/composed/PageHeader";
import { DeltaText } from "../components/composed/DeltaText";
import { KpiTile } from "../components/composed/KpiTile";
import { DataTable } from "../components/composed/DataTable";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Skeleton } from "../components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "../components/ui/toggle-group";
import { LineChart } from "../components/charts/LineChart";
import { DonutChart } from "../components/charts/DonutChart";
import { DkbSyncButton } from "../components/portfolio/DkbSyncButton";
import { BrokerBadge, BrokerFilterToggle, matchesBroker, useBrokerFilter } from "../components/portfolio/BrokerFilter";
import { RegimeBadge } from "../components/portfolio/RegimeBadge";
import { type PortfolioRange, usePortfolioHistory } from "../hooks/usePortfolioHistory";
import { usePriceMoves } from "../hooks/usePriceMoves";
import { useIsPhone } from "../hooks/useMediaQuery";

const ranges: PortfolioRange[] = ["1W", "1M", "3M", "YTD", "1Y", "All"];

const activityColumns: ColumnDef<PortfolioActivity>[] = [
  { accessorKey: "date", header: "Date", cell: ({ row }) => formatDate(row.original.date) },
  { accessorKey: "description", header: "Description" },
  { accessorKey: "source", header: "Source" },
  {
    accessorKey: "amount",
    header: "Amount",
    cell: ({ row }) => <span className="font-mono tabular-nums">{formatCurrency(Number(row.original.amount), row.original.currency)}</span>,
  },
];

function allocationSlices(positions: WealthSummary["positions"]) {
  const totals = positions.reduce<Record<string, number>>((next, item) => {
    const key = item.asset_type ?? (item.source === "manual" ? "other" : "broker securities");
    next[key] = (next[key] ?? 0) + Number(item.current_value ?? 0);
    return next;
  }, {});
  return Object.entries(totals).map(([name, value]) => ({ name, value }));
}

export function Dashboard() {
  const isPhone = useIsPhone();
  const [range, setRange] = useState<PortfolioRange>("1M");
  const wealth = useQuery({ queryKey: ["wealth"], queryFn: () => api<WealthSummary>("/api/portfolio/wealth") });
  const activity = useQuery({ queryKey: ["portfolio-activity"], queryFn: () => api<PortfolioActivity[]>("/api/portfolio/activity") });
  const history = usePortfolioHistory(range);
  const snapshots = history.data ?? [];
  const latest = snapshots.at(-1);
  const previous = snapshots.at(-2);
  const dayDelta = latest && previous ? latest.total_value - previous.total_value : null;
  const reportCurrency = wealth.data?.currency ?? "EUR";
  const delta = formatSignedDelta(dayDelta, "currency", { currency: latest?.currency ?? reportCurrency });
  const positions = wealth.data?.positions ?? [];
  const brokerFilter = useBrokerFilter(positions.map((position) => position.source));
  // The broker filter narrows the position views only; net worth stays the whole book.
  const shownPositions = positions.filter((position) => matchesBroker(position.source, brokerFilter.filter));
  const moves = usePriceMoves(shownPositions.map((position) => position.symbol));
  const slices = allocationSlices(shownPositions);
  const largestPositions = [...shownPositions]
    .map((position) => ({ position, move: position.symbol ? moves.data?.[position.symbol] : undefined }))
    .sort((a, b) => b.position.current_value - a.position.current_value)
    .slice(0, 5);
  const syncedAccounts = wealth.data?.accounts?.filter((item) => item.source === "dkb" || item.source === "scalable") ?? [];
  const cashflowNet = wealth.data?.cashflow_30d?.net;

  return (
    <div className="space-y-6">
      {/* One instance only (DkbSyncButton owns the sync session): beside the title from sm up,
          a full-width block under it on a phone, where the header row has no room for it. */}
      <PageHeader title="Overview" actions={isPhone ? undefined : <div className="flex items-center gap-3"><RegimeBadge /><DkbSyncButton showState /></div>} />
      {isPhone && (
        <div className="flex flex-col gap-3">
          <RegimeBadge />
          <DkbSyncButton showState />
        </div>
      )}

      {wealth.isError && (
        <Card>
          <CardContent className="flex items-center justify-between gap-3">
            <div className="text-sm text-danger">Failed to load portfolio data. Please try again later.</div>
            <button onClick={() => wealth.refetch()} className="rounded-md bg-danger/10 px-3 py-1.5 text-xs font-medium text-danger transition-colors hover:bg-danger/20">Retry</button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="flex-col items-start justify-between gap-3 space-y-0 sm:flex-row sm:items-center">
          <CardTitle>Portfolio value</CardTitle>
          <ToggleGroup type="single" value={range} className="max-w-full flex-wrap justify-start" onValueChange={(value) => value && setRange(value as PortfolioRange)}>
            {ranges.map((item) => <ToggleGroupItem key={item} value={item} size="sm">{item}</ToggleGroupItem>)}
          </ToggleGroup>
        </CardHeader>
        <CardContent>
          {history.isLoading ? (
            <Skeleton className="h-[260px] w-full sm:h-[360px]" />
          ) : (
            <LineChart
              height={360}
              ariaLabel="Portfolio value over time"
              series={[{ name: "Portfolio value", data: snapshots.map((item) => [item.date, item.total_value]), color: "#FA8001" }]}
              yFormat={(value) => formatCurrency(value, reportCurrency, { compact: true })}
              tooltipFormat={(value) => formatCurrency(value, reportCurrency)}
              area
            />
          )}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        {wealth.isLoading ? (
          <>
            <Skeleton className="h-[88px] w-full" />
            <Skeleton className="h-[88px] w-full" />
            <Skeleton className="h-[88px] w-full" />
            <Skeleton className="h-[88px] w-full" />
          </>
        ) : (
          <>
            <KpiTile label="Net worth" value={formatCurrency(wealth.data?.total_value, reportCurrency)} glossaryKey="net_worth" sparkline={snapshots.map((item) => item.total_value)} />
            <KpiTile label="Today's change" value={<DeltaText delta={delta} />} tone={delta.tone === "down" ? "bad" : delta.tone === "up" ? "good" : "neutral"} />
            <KpiTile label="30-day cashflow" value={formatCurrency(cashflowNet, reportCurrency, { signed: true })} tone={(cashflowNet ?? 0) < 0 ? "bad" : "good"} />
            <KpiTile label="Holdings" value={String(positions.length)} />
          </>
        )}
      </div>

      <BrokerFilterToggle brokers={brokerFilter.brokers} value={brokerFilter.filter} onChange={brokerFilter.setFilter} />
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Allocation by asset type</CardTitle></CardHeader>
          <CardContent>
            {wealth.isLoading ? (
              <Skeleton className="h-[300px] w-full" />
            ) : (
              <DonutChart className="h-72" ariaLabel="Allocation by asset type" slices={slices} />
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Largest positions</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            {wealth.isLoading ? (
              <>
                {[...Array(5)].map((_, i) => (
                  <div key={i} className="flex items-center justify-between gap-3 border-b border-border pb-3 last:border-0 last:pb-0">
                    <div className="min-w-0 space-y-1.5">
                      <Skeleton className="h-4 w-32" />
                      <Skeleton className="h-3 w-20" />
                    </div>
                    <div className="space-y-1.5 text-right">
                      <Skeleton className="ml-auto h-4 w-24" />
                      <Skeleton className="ml-auto h-3 w-16" />
                    </div>
                  </div>
                ))}
              </>
            ) : (
              <>
                {largestPositions.map(({ position, move }) => (
                  <div key={`${position.source}-${position.id}`} className="flex items-center justify-between gap-3 border-b border-border pb-3 last:border-0 last:pb-0">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{position.name}</div>
                      <div className="flex items-center gap-1.5 font-mono text-xs text-text-secondary">
                        <span className="truncate">{position.symbol ?? position.isin ?? position.source}</span>
                        <BrokerBadge source={position.source} />
                      </div>
                    </div>
                    <div className="text-right font-mono text-sm tabular-nums">
                      <div>{formatCurrency(position.current_value, position.currency)}</div>
                      <div className="text-xs">{move?.deltaPct == null ? <span className="text-text-muted">No day move</span> : <DeltaText delta={formatSignedDelta(move.deltaPct, "percent")} />}</div>
                    </div>
                  </div>
                ))}
                {!largestPositions.length && <div className="text-sm text-text-muted">No positions mirrored yet.</div>}
              </>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Recent activity</CardTitle></CardHeader>
        <CardContent>
          {activity.isError ? (
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm text-danger">Failed to load activity data. Please try again later.</div>
              <button onClick={() => activity.refetch()} className="rounded-md bg-danger/10 px-3 py-1.5 text-xs font-medium text-danger transition-colors hover:bg-danger/20">Retry</button>
            </div>
          ) : activity.isLoading ? (
            <div className="space-y-3">
              {[...Array(5)].map((_, i) => (
                <div key={i} className="flex items-center gap-4">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-4 flex-1" />
                  <Skeleton className="h-4 w-20" />
                  <Skeleton className="h-4 w-28" />
                </div>
              ))}
            </div>
          ) : (
            <DataTable data={(activity.data ?? []).slice(0, 10)} columns={activityColumns} emptyState="No mirrored activity yet." />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Synced depots</CardTitle></CardHeader>
        <CardContent className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2 xl:grid-cols-5">
          <MirrorMetric label="Accounts mirrored" value={String(syncedAccounts.length)} />
          <MirrorMetric label="DKB securities value" value={formatCurrency(wealth.data?.dkb_security_value, reportCurrency)} />
          <MirrorMetric label="Scalable securities value" value={formatCurrency(wealth.data?.broker_security_value ?? 0, reportCurrency)} />
          <MirrorMetric label="Manual external value" value={formatCurrency(wealth.data?.manual_value, reportCurrency)} />
          <MirrorMetric label="Snapshot" value={wealth.data?.last_snapshot ? formatDate(wealth.data.last_snapshot) : "Not recorded"} />
        </CardContent>
      </Card>
    </div>
  );
}

function MirrorMetric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-md border border-border bg-surface-2 p-3"><div className="text-xs text-text-secondary">{label}</div><div className="mt-1 font-mono tabular-nums">{value}</div></div>;
}
