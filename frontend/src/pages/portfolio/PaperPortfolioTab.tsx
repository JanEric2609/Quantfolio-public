import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api, type PaperPortfolioSummary, type PaperHolding, type PaperTrade } from "../../lib/api";
import { formatCurrency, formatPercent, formatNumber, formatDate, formatSignedDelta } from "../../lib/format";
import { DeltaText } from "../../components/composed/DeltaText";
import { DataTable } from "../../components/composed/DataTable";
import { KpiTile } from "../../components/composed/KpiTile";
import { annualisedSample, TOO_EARLY } from "../../lib/sampleSize";
import { Badge } from "../../components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { Button } from "../../components/ui/button";
import { PaperRunCard } from "./components/PaperRunCard";

export function PaperPortfolioTab() {
  const summary = useQuery({
    queryKey: ["paper-portfolio"],
    queryFn: () => api<PaperPortfolioSummary>("/api/paper-portfolio/"),
  });
  const holdings = useQuery({
    queryKey: ["paper-portfolio-holdings"],
    queryFn: () => api<PaperHolding[]>("/api/paper-portfolio/holdings"),
  });
  const trades = useQuery({
    queryKey: ["paper-portfolio-trades"],
    queryFn: () => api<PaperTrade[]>("/api/paper-portfolio/trades"),
  });

  const s = summary.data;

  const holdingsColumns: ColumnDef<PaperHolding>[] = useMemo(
    () => [
      {
        accessorKey: "ticker",
        header: "Ticker",
        cell: ({ row }) => <span className="font-mono">{row.original.ticker}</span>,
      },
      { accessorKey: "name", header: "Name" },
      {
        accessorKey: "asset_type",
        header: "Asset type",
        cell: ({ row }) => <Badge variant="secondary">{row.original.asset_type}</Badge>,
      },
      {
        accessorKey: "quantity",
        header: "Quantity",
        cell: ({ row }) => <span className="font-mono tabular-nums">{formatNumber(row.original.quantity, { decimals: 4 })}</span>,
      },
      {
        accessorKey: "avg_buy_price",
        header: "Avg Buy",
        cell: ({ row }) => formatCurrency(row.original.avg_buy_price, { currency: row.original.currency }),
      },
      {
        accessorKey: "current_price",
        header: "Current Price",
        cell: ({ row }) => (
          <span className="font-mono tabular-nums">
            {row.original.current_price != null ? formatCurrency(row.original.current_price, { currency: row.original.currency }) : "—"}
            {row.original.priced === false && (
              <Badge variant="warning" className="ml-1 text-[10px]" title="No EUR price today: valued at cost">at cost</Badge>
            )}
          </span>
        ),
      },
      {
        accessorKey: "market_value",
        header: "Market Value",
        cell: ({ row }) => (
          <span className="font-mono tabular-nums">
            {row.original.market_value != null ? formatCurrency(row.original.market_value, { currency: row.original.currency }) : "—"}
          </span>
        ),
      },
      {
        accessorKey: "unrealized_pnl",
        header: "Unrealized P&L",
        cell: ({ row }) => {
          const pnl = row.original.unrealized_pnl;
          if (pnl == null) return "—";
          return (
            <DeltaText delta={formatSignedDelta(pnl, "currency", { currency: row.original.currency })} />
          );
        },
      },
      {
        accessorKey: "return_pct",
        header: "Return %",
        cell: ({ row }) => {
          const pct = row.original.return_pct;
          if (pct == null) return "—";
          return <DeltaText delta={formatSignedDelta(pct, "percent")} />;
        },
      },
      {
        accessorKey: "weight_pct",
        header: "Weight %",
        cell: ({ row }) => <span className="font-mono tabular-nums">{formatPercent(row.original.weight_pct)}</span>,
      },
    ],
    []
  );

  const tradesColumns: ColumnDef<PaperTrade>[] = useMemo(
    () => [
      {
        accessorKey: "date",
        header: "Date",
        cell: ({ row }) => formatDate(row.original.date),
      },
      {
        accessorKey: "ticker",
        header: "Ticker",
        cell: ({ row }) => <span className="font-mono">{row.original.ticker}</span>,
      },
      {
        accessorKey: "side",
        header: "Side",
        cell: ({ row }) => (
          <Badge variant={row.original.side === "buy" ? "success" : "danger"}>{row.original.side.toUpperCase()}</Badge>
        ),
      },
      {
        accessorKey: "quantity",
        header: "Quantity",
        cell: ({ row }) => <span className="font-mono tabular-nums">{formatNumber(row.original.quantity, { decimals: 4 })}</span>,
      },
      {
        accessorKey: "price",
        header: "Price",
        cell: ({ row }) => formatCurrency(row.original.price),
      },
      {
        accessorKey: "value",
        header: "Value",
        cell: ({ row }) => formatCurrency(row.original.value),
      },
      {
        accessorKey: "fee",
        header: "Fee",
        cell: ({ row }) => (row.original.fee != null ? formatCurrency(row.original.fee) : "—"),
      },
      {
        accessorKey: "confidence",
        header: "Confidence",
        cell: ({ row }) =>
          row.original.confidence != null ? (
            <span className="font-mono tabular-nums">{formatPercent(row.original.confidence)}</span>
          ) : (
            "—"
          ),
      },
      {
        accessorKey: "rationale",
        header: "Rationale",
        cell: ({ row }) => row.original.rationale ?? "—",
      },
    ],
    []
  );

  if (summary.isError || holdings.isError || trades.isError) {
    return (
      <Card>
        <CardContent className="flex items-center justify-between gap-3 pt-6">
          <div className="text-sm text-danger">Failed to load paper portfolio data.</div>
          <Button variant="outline" size="sm" onClick={() => { summary.refetch(); holdings.refetch(); trades.refetch(); }}>Retry</Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-5">
      {summary.isLoading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[...Array(8)].map((_, i) => <Skeleton key={i} className="h-[88px] w-full" />)}
        </div>
      ) : s && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <KpiTile label="Total Value" value={formatCurrency(s.total_value, { currency: s.currency })} />
          <KpiTile label="Cash Balance" value={formatCurrency(s.cash_balance, { currency: s.currency })} />
          <KpiTile label="Securities Value" value={formatCurrency(s.securities_value, { currency: s.currency })} />
          <KpiTile
            label="Total Return"
            value={formatPercent(s.total_return_pct, { signed: true })}
            tone={s.total_return_pct < 0 ? "bad" : s.total_return_pct > 0 ? "good" : "neutral"}
          />
          <KpiTile
            label="Sharpe (annualised)"
            value={
              s.history_days != null && !annualisedSample(s.history_days).enough
                ? TOO_EARLY
                : s.sharpe != null ? formatNumber(s.sharpe, { decimals: 2 }) : "—"
            }
          />
          <KpiTile label="Max Drawdown" value={s.max_drawdown != null ? formatPercent(s.max_drawdown) : "—"} {...(s.max_drawdown != null ? { tone: "bad" as const } : {})} />
          <KpiTile label="Holdings" value={s.holding_count} />
          <KpiTile label="Trades" value={s.trade_count} />
        </div>
      )}

      {s?.id && (
        <PaperRunCard
          portfolioId={s.id}
          invalidate={[["paper-portfolio"], ["paper-portfolio-holdings"], ["paper-portfolio-trades"]]}
        />
      )}

      {s?.history_days != null && (
        <p className="text-xs text-text-secondary">Sharpe: {annualisedSample(s.history_days).note}.</p>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Holdings</CardTitle>
        </CardHeader>
        <CardContent>
          {holdings.isLoading ? (
            <div className="space-y-3">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
          ) : (
            <DataTable
              data={holdings.data ?? []}
              columns={holdingsColumns}
              enableFilter
              csvFilename="paper-holdings.csv"
              emptyState="No paper holdings yet. Add a simulated trade to get started."
            />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Trades</CardTitle>
        </CardHeader>
        <CardContent>
          {trades.isLoading ? (
            <div className="space-y-3">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
          ) : (
            <DataTable
              data={trades.data ?? []}
              columns={tradesColumns}
              enableFilter
              csvFilename="paper-trades.csv"
              emptyState="No trades yet."
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
