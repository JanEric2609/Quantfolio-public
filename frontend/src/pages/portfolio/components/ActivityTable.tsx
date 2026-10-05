import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api, type PortfolioActivity } from "../../../lib/api";
import { formatCurrency, formatDate } from "../../../lib/format";
import { Badge } from "../../../components/ui/badge";
import { Card, CardContent } from "../../../components/ui/card";
import { Skeleton } from "../../../components/ui/skeleton";
import { DataTable } from "../../../components/composed/DataTable";
import { Input } from "../../../components/ui/input";

const _SKELETON_COUNT = 5;

const columns: ColumnDef<PortfolioActivity>[] = [
  { accessorKey: "date", header: "Date", cell: ({ row }) => formatDate(row.original.date) },
  { accessorKey: "description", header: "Description" },
  { accessorKey: "source", header: "Source" },
  { accessorKey: "amount", header: "Amount", cell: ({ row }) => <span className="font-mono">{formatCurrency(Number(row.original.amount), { currency: row.original.currency })}</span> },
  { accessorKey: "isin", header: "ISIN", cell: ({ row }) => <span className="font-mono">{row.original.isin ?? "-"}</span> },
  { accessorKey: "symbol", header: "Symbol", cell: ({ row }) => <span className="font-mono">{row.original.symbol ?? "-"}</span> },
  { accessorKey: "quantity", header: "Quantity", cell: ({ row }) => row.original.quantity ?? "-" },
  { accessorKey: "price", header: "Price", cell: ({ row }) => row.original.price ?? "-" },
  { accessorKey: "review_state", header: "Review", cell: ({ row }) => <Badge variant={row.original.review_state === "trusted" ? "success" : "warning"}>{row.original.review_state}</Badge> },
];

export function ActivityTable() {
  const activity = useQuery({ queryKey: ["portfolio-activity"], queryFn: () => api<PortfolioActivity[]>("/api/portfolio/activity") });
  const [ticker, setTicker] = useState("");
  const [account, setAccount] = useState("");
  const [from, setFrom] = useState("");
  const rows = useMemo(() => (activity.data ?? []).filter((row) => {
    if (ticker && !`${row.symbol ?? ""} ${row.isin ?? ""}`.toLowerCase().includes(ticker.toLowerCase())) return false;
    if (account && !`${row.connected_account_id ?? ""} ${row.source}`.toLowerCase().includes(account.toLowerCase())) return false;
    return !from || row.date >= from;
  }), [account, activity.data, from, ticker]);
  return (
    <Card><CardContent className="space-y-3 pt-6">
      <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
        <Input value={ticker} onChange={(event) => setTicker(event.target.value)} placeholder="Ticker or ISIN filter" />
        <Input value={account} onChange={(event) => setAccount(event.target.value)} placeholder="Account or source filter" />
        <Input value={from} onChange={(event) => setFrom(event.target.value)} type="date" aria-label="Activity from date" />
      </div>
      {activity.isLoading ? (
        <div className="space-y-3">{[...Array(_SKELETON_COUNT)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
      ) : activity.isError ? (
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm text-danger">Failed to load activity data.</div>
          <button onClick={() => activity.refetch()} className="rounded-md bg-danger/10 px-3 py-1.5 text-xs font-medium text-danger transition-colors hover:bg-danger/20">Retry</button>
        </div>
      ) : (
        <DataTable data={rows} columns={columns} csvFilename="portfolio-activity.csv" enableFilter emptyState="No activity matches the filters." />
      )}
    </CardContent></Card>
  );
}
