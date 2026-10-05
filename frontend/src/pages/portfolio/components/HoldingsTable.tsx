import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api, type Holding, type WealthSummary } from "../../../lib/api";
import { formatCurrency, formatNumber, formatPercent, formatSignedDelta } from "../../../lib/format";
import { DeltaText } from "../../../components/composed/DeltaText";
import { DataTable } from "../../../components/composed/DataTable";
import { Card, CardContent } from "../../../components/ui/card";
import { Skeleton } from "../../../components/ui/skeleton";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { useQuotes } from "../../../hooks/useQuotes";
import { usePriceMoves } from "../../../hooks/usePriceMoves";
import { HoldingDetailSheet } from "./HoldingDetailSheet";
import { BrokerBadge } from "../../../components/portfolio/BrokerFilter";

const SYNC_SOURCES: Record<string, string> = { dkb_sync: "dkb", broker_sync: "scalable" };

/**
 * Where a holding sits: the depots of the synced positions with its ISIN (a
 * synced holding sums every depot), else what its own source says. A
 * hand-entered holding is "manual" even when a depot holds the same ISIN.
 */
export function heldAt(holding: Pick<Holding, "isin" | "source">, depotsByIsin: Map<string, string[]>): string[] {
  const synced = holding.source ? SYNC_SOURCES[holding.source] : undefined;
  if (!synced) return ["manual"];
  return (holding.isin && depotsByIsin.get(holding.isin)) || [synced];
}

export function resolveLatestPrice(quotePrice: number | undefined, avgBuyPriceRaw: string | null): number | null {
  if (quotePrice != null) return quotePrice;
  if (avgBuyPriceRaw == null) return null;
  const parsed = Number(avgBuyPriceRaw);
  return Number.isFinite(parsed) ? parsed : null;
}

export type HoldingRow = Holding & {
  latestPrice: number | null;
  marketValue: number | null;
  // Currency the market value is actually in: the live quote's currency when a
  // quote drove the price, else the holding's own currency. No FX conversion
  // exists, so labelling a USD-quote value with the holding's EUR symbol would
  // be a silent mislabel.
  valueCurrency: string;
  quoteStale: boolean;
  dayPnlPct?: number;
  weight: number;
  heldAt: string[];
};

export function HoldingsTable({ rightSlot }: { rightSlot?: React.ReactNode }) {
  const [selected, setSelected] = useState<Holding | null>(null);
  const holdings = useQuery({ queryKey: ["holdings"], queryFn: () => api<Holding[]>("/api/portfolio/holdings") });
  // Shares the Overview's cache; only the depot of each synced position is read.
  const wealth = useQuery({ queryKey: ["wealth"], queryFn: () => api<WealthSummary>("/api/portfolio/wealth") });
  const quotes = useQuotes((holdings.data ?? []).map((holding) => holding.ticker));
  const moves = usePriceMoves((holdings.data ?? []).map((holding) => holding.ticker));
  const rows = useMemo(() => {
    const depotsByIsin = new Map<string, string[]>();
    for (const position of wealth.data?.positions ?? []) {
      if (!position.isin || (position.source !== "dkb" && position.source !== "scalable")) continue;
      const depots = depotsByIsin.get(position.isin) ?? [];
      if (!depots.includes(position.source)) depots.push(position.source);
      depotsByIsin.set(position.isin, depots.sort());
    }
    const valued = (holdings.data ?? []).map((holding) => {
      const quote = holding.ticker ? quotes.data?.[holding.ticker] : undefined;
      const latestPrice = resolveLatestPrice(quote?.price, holding.avg_buy_price);
      const marketValue = latestPrice != null ? Number(holding.quantity) * latestPrice : null;
      return { ...holding, latestPrice, marketValue, valueCurrency: quote?.currency ?? holding.currency, quoteStale: quote?.stale ?? true, dayPnlPct: holding.ticker ? moves.data?.[holding.ticker]?.deltaPct : undefined, weight: 0, heldAt: heldAt(holding, depotsByIsin) };
    });
    const total = valued.reduce((sum, row) => sum + (row.marketValue ?? 0), 0);
    return valued.map((row) => ({ ...row, weight: total ? (row.marketValue ?? 0) / total : 0 }));
  }, [holdings.data, moves.data, quotes.data, wealth.data]);
  const columns: ColumnDef<HoldingRow>[] = [
    { accessorKey: "ticker", header: "Ticker", cell: ({ row }) => <span className="font-mono">{row.original.ticker}</span> },
    { accessorKey: "name", header: "Name" },
    { id: "heldAt", accessorFn: (row) => row.heldAt.join(" "), header: "Held at", cell: ({ row }) => <span className="inline-flex gap-1">{row.original.heldAt.map((source) => <BrokerBadge key={source} source={source} />)}</span> },
    { accessorKey: "isin", header: "ISIN", cell: ({ row }) => <span className="font-mono">{row.original.isin ?? "-"}</span> },
    { accessorKey: "asset_type", header: "Asset type", cell: ({ row }) => <Badge variant="secondary">{row.original.asset_type}</Badge> },
    { accessorKey: "quantity", header: "Quantity", cell: ({ row }) => <span className="font-mono tabular-nums">{formatNumber(Number(row.original.quantity), { digits: 4, minDigits: 0 })}</span> },
    { accessorKey: "avg_buy_price", header: "Avg buy", cell: ({ row }) => row.original.avg_buy_price != null ? formatCurrency(Number(row.original.avg_buy_price), { currency: row.original.currency }) : <span className="text-text-muted">Unknown</span> },
    { accessorKey: "marketValue", header: "Market value", cell: ({ row }) => <span className="inline-flex items-center gap-1 font-mono">{formatCurrency(row.original.marketValue, { currency: row.original.valueCurrency })}{row.original.quoteStale && <Badge variant="warning">stale</Badge>}</span> },
    { accessorKey: "dayPnlPct", header: "Day P&L %", cell: ({ row }) => row.original.dayPnlPct == null ? "—" : <DeltaText delta={formatSignedDelta(row.original.dayPnlPct, "percent")} /> },
    { accessorKey: "weight", header: "Weight %", cell: ({ row }) => formatPercent(row.original.weight) },
  ];
  // One card per holding below sm: the nine-column table would truncate every cell on a phone.
  const mobileCard = (row: HoldingRow) => (
    <span className="block space-y-1.5">
      <span className="flex items-start justify-between gap-3">
        <span className="min-w-0">
          <span className="flex items-center gap-1">
            <span className="truncate font-mono text-sm font-semibold text-text-primary">{row.ticker ?? row.isin ?? "—"}</span>
            {row.heldAt.map((source) => <BrokerBadge key={source} source={source} />)}
          </span>
          <span className="block truncate text-xs text-text-secondary">{row.name}</span>
        </span>
        <span className="shrink-0 text-right">
          <span className="block font-mono text-sm tabular-nums text-text-primary">{formatCurrency(row.marketValue, row.valueCurrency)}</span>
          {row.quoteStale && <span className="mt-0.5 inline-block rounded-full bg-warn/15 px-1.5 text-[10px] font-medium text-warn">stale</span>}
        </span>
      </span>
      <span className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-text-secondary">
        <span className="tabular-nums">
          {formatNumber(Number(row.quantity), { digits: 4, minDigits: 0 })} × {row.avg_buy_price != null ? formatCurrency(Number(row.avg_buy_price), row.currency) : "avg buy unknown"}
        </span>
        <span className="flex items-center gap-2 tabular-nums">
          <span>{formatPercent(row.weight, { digits: 1 })}</span>
          {row.dayPnlPct != null && <DeltaText delta={formatSignedDelta(row.dayPnlPct, "percent")} />}
        </span>
      </span>
    </span>
  );
  return (
    <>
      <Card><CardContent className="pt-6">
        {holdings.isLoading ? (
          <div className="space-y-3">
            {[...Array(5)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
          </div>
        ) : holdings.isError ? (
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm text-danger">Failed to load holdings data.</div>
            <Button variant="outline" size="sm" onClick={() => holdings.refetch()}>Retry</Button>
          </div>
        ) : (
          <DataTable data={rows} columns={columns} onRowClick={(row) => setSelected(row)} mobileCard={mobileCard} enableFilter csvFilename="portfolio-holdings.csv" rightSlot={rightSlot} emptyState="No manual holdings yet." />
        )}
      </CardContent></Card>
      <HoldingDetailSheet holding={selected} onOpenChange={(open) => !open && setSelected(null)} />
    </>
  );
}
