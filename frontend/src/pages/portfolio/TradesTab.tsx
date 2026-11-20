import { useQuery } from "@tanstack/react-query";
import { ColumnDef } from "@tanstack/react-table";
import { DataTable } from "../../components/composed/DataTable";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { formatDate, formatCurrency } from "../../lib/format";
import { api, type PortfolioTransaction } from "../../lib/api";

export function TradesTab() {
  const txns = useQuery({
    queryKey: ["transactions"],
    queryFn: () => api<PortfolioTransaction[]>("/api/portfolio/transactions"),
  });

  const columns: ColumnDef<PortfolioTransaction>[] = [
    {
      accessorKey: "date",
      header: "Date",
      cell: ({ row }) => formatDate(row.getValue("date")),
    },
    {
      accessorKey: "type",
      header: "Type",
      cell: ({ row }) => {
        const t = String(row.getValue("type"));
        return (
          <Badge variant={t.toLowerCase().includes("buy") ? "success" : "danger"}>
            {t.toUpperCase()}
          </Badge>
        );
      },
    },
    { accessorKey: "quantity", header: "Quantity" },
    {
      accessorKey: "price",
      header: "Price",
      cell: ({ row }) => formatCurrency(Number(row.getValue("price"))),
    },
    {
      accessorKey: "fees",
      header: "Fees",
      cell: ({ row }) => formatCurrency(Number(row.getValue("fees"))),
    },
    { accessorKey: "notes", header: "Notes" },
  ];

  if (txns.isLoading) {
    return <div className="space-y-3">{[...Array(5)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>;
  }

  if (txns.isError) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface p-4">
        <div className="text-sm text-danger">Failed to load trade data.</div>
        <button onClick={() => txns.refetch()} className="rounded-md bg-danger/10 px-3 py-1.5 text-xs font-medium text-danger transition-colors hover:bg-danger/20">Retry</button>
      </div>
    );
  }

  return (
    <DataTable
      data={txns.data ?? []}
      columns={columns}
      enableFilter
      emptyState={<div className="text-center text-text-secondary py-8">No trades recorded yet.</div>}
    />
  );
}
