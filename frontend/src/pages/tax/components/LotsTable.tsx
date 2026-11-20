import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type TaxLot } from "../../../lib/api";
import { DataTable } from "../../../components/composed/DataTable";
import { Button } from "../../../components/ui/button";
import { formatCurrency, formatDate, formatPercent } from "../../../lib/format";
import type { ColumnDef } from "@tanstack/react-table";

const columns: ColumnDef<TaxLot>[] = [
  { accessorKey: "isin", header: "ISIN", cell: ({ row }) => <span className="font-mono text-xs">{row.original.isin}</span> },
  { accessorKey: "name", header: "Name", cell: ({ row }) => row.original.name ?? row.original.symbol ?? "—" },
  { accessorKey: "fund_class", header: "Fund class", cell: ({ row }) => <span className="capitalize">{row.original.fund_class}</span> },
  { accessorKey: "teilfreistellung_pct", header: "Teilfreistellung", cell: ({ row }) => formatPercent(row.original.teilfreistellung_pct, { digits: 0 }) },
  { accessorKey: "acquired_at", header: "Acquired", cell: ({ row }) => formatDate(row.original.acquired_at, { style: "short" }) },
  { accessorKey: "quantity_remaining", header: "Qty (remaining)", cell: ({ row }) => `${row.original.quantity_remaining} / ${row.original.quantity_initial}` },
  { accessorKey: "cost_basis_eur", header: "Cost basis", cell: ({ row }) => formatCurrency(row.original.cost_basis_eur, "EUR") },
  { accessorKey: "source", header: "Source" },
];

export function LotsTable({ onRowClick }: { onRowClick?: (lot: TaxLot) => void }) {
  const queryClient = useQueryClient();

  const lots = useQuery({
    queryKey: ["tax", "lots"],
    queryFn: () => api<{ items: TaxLot[]; estimate: boolean }>("/api/tax/lots"),
  });

  const seedLots = useMutation({
    mutationFn: () => api<{ created: number }>("/api/tax/lots/seed-from-activity", { method: "POST" }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["tax", "lots"] });
      toast.success(`Seeded ${data.created} lots from activity ledger`);
    },
    onError: (err: Error) => toast.error(`Failed to seed lots: ${err.message}`),
  });

  const items = lots.data?.items ?? [];

  return (
    <DataTable
      data={items}
      columns={columns}
      onRowClick={onRowClick}
      enableFilter
      csvFilename="tax-lots.csv"
      emptyState={<span>No lots yet. Seed from the activity ledger or add lots manually via the API.</span>}
      rightSlot={
        <Button variant="outline" size="sm" onClick={() => seedLots.mutate()} disabled={seedLots.isPending}>
          {seedLots.isPending ? "Seeding…" : "Seed from activity ledger"}
        </Button>
      }
    />
  );
}
