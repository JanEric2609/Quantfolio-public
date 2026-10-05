import { useMemo } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { DataTable } from "../../../components/composed/DataTable";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Switch } from "../../../components/ui/switch";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "../../../components/ui/alert-dialog";
import { formatCurrency, formatDate } from "../../../lib/format";

export interface IncomeSourceRow {
  id: string;
  name: string;
  amount: string;
  currency: string;
  cadence: string;
  next_date: string;
  active: boolean;
}

function monthlyAmount(row: IncomeSourceRow) {
  const amount = Number(row.amount);
  return row.cadence === "weekly" ? amount * 52 / 12 : row.cadence === "quarterly" ? amount / 3 : row.cadence === "annual" ? amount / 12 : amount;
}

export function IncomeTable({ data, onToggleActive, onDelete, rightSlot, loading }: {
  data: IncomeSourceRow[];
  onToggleActive?: (row: IncomeSourceRow) => void;
  onDelete?: (id: string) => void;
  rightSlot?: React.ReactNode;
  loading?: boolean;
}) {
  const columns: ColumnDef<IncomeSourceRow>[] = useMemo(() => [
    { accessorKey: "name", header: "Name" },
    { accessorKey: "amount", header: "Amount", cell: (c) => {
      const row = c.row.original;
      return formatCurrency(Number(row.amount), { currency: row.currency });
    }},
    { id: "monthly", header: "Monthly", cell: (c) => formatCurrency(monthlyAmount(c.row.original), { currency: c.row.original.currency }) },
    { accessorKey: "cadence", header: "Cadence", cell: (c) => <Badge variant="secondary">{c.getValue() as string}</Badge> },
    { accessorKey: "next_date", header: "Next", cell: (c) => formatDate(c.getValue() as string) },
    {
      id: "active",
      header: "Status",
      cell: (c) => <div className="flex items-center gap-2"><Switch checked={c.row.original.active} onCheckedChange={() => onToggleActive?.(c.row.original)} /><span>{c.row.original.active ? "Active" : "Paused"}</span></div>,
    },
    {
      id: "actions",
      header: "",
      cell: (c) => (
        <div className="flex gap-1">
          {onDelete && <AlertDialog><AlertDialogTrigger asChild><Button variant="ghost" size="sm">Delete</Button></AlertDialogTrigger><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Delete income source?</AlertDialogTitle><AlertDialogDescription>This removes it from the money-to-budget total.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={() => onDelete(c.row.original.id)}>Delete</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>}
        </div>
      ),
    },
  ], [onToggleActive, onDelete]);

  if (loading) {
    return <div className="text-sm text-text-muted py-8 text-center">Loading income sources…</div>;
  }

  return (
    <DataTable
      data={data}
      columns={columns}
      enableFilter
      emptyState={<span className="text-text-muted">No income sources.</span>}
      rightSlot={rightSlot}
      csvFilename="income-sources.csv"
    />
  );
}
