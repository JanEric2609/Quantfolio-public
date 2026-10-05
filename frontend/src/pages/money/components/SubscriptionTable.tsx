import { useMemo } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { DataTable } from "../../../components/composed/DataTable";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Switch } from "../../../components/ui/switch";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "../../../components/ui/alert-dialog";
import { formatCurrency, formatDate } from "../../../lib/format";

export interface SubscriptionRow {
  id: string;
  name: string;
  amount: string;
  currency: string;
  billing_cycle: string;
  next_due_date: string;
  category_id?: string;
  payment_method?: string;
  active: boolean;
  notes?: string;
}

function monthlyAmount(row: SubscriptionRow) {
  const amount = Number(row.amount);
  return row.billing_cycle === "weekly" ? amount * 52 / 12 : row.billing_cycle === "quarterly" ? amount / 3 : row.billing_cycle === "annual" ? amount / 12 : amount;
}

export function SubscriptionTable({ data, categories = {}, onMarkPaid, onToggleActive, onDelete, rightSlot, loading }: {
  data: SubscriptionRow[];
  categories?: Record<string, string>;
  onMarkPaid?: (id: string) => void;
  onToggleActive?: (row: SubscriptionRow) => void;
  onDelete?: (id: string) => void;
  rightSlot?: React.ReactNode;
  loading?: boolean;
}) {
  const columns: ColumnDef<SubscriptionRow>[] = useMemo(() => [
    { accessorKey: "name", header: "Name" },
    { accessorKey: "amount", header: "Amount", cell: (c) => {
      const row = c.row.original;
      return formatCurrency(Number(row.amount), { currency: row.currency });
    }},
    { id: "monthly", header: "Monthly", cell: (c) => formatCurrency(monthlyAmount(c.row.original), { currency: c.row.original.currency }) },
    { accessorKey: "billing_cycle", header: "Cycle", cell: (c) => <Badge variant="secondary">{c.getValue() as string}</Badge> },
    { accessorKey: "category_id", header: "Category", cell: (c) => categories[c.getValue() as string] ?? "Uncategorized" },
    { accessorKey: "next_due_date", header: "Next Due", cell: (c) => formatDate(c.getValue() as string) },
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
          {onMarkPaid && <Button variant="ghost" size="sm" onClick={() => onMarkPaid(c.row.original.id)}>Paid</Button>}
          {onDelete && <AlertDialog><AlertDialogTrigger asChild><Button variant="ghost" size="sm">Delete</Button></AlertDialogTrigger><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Delete subscription?</AlertDialogTitle><AlertDialogDescription>This removes the subscription schedule.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={() => onDelete(c.row.original.id)}>Delete</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>}
        </div>
      ),
    },
  ], [categories, onMarkPaid, onToggleActive, onDelete]);

  if (loading) {
    return <div className="text-sm text-text-muted py-8 text-center">Loading subscriptions…</div>;
  }

  return (
    <DataTable
      data={data}
      columns={columns}
      enableFilter
      emptyState={<span className="text-text-muted">No subscriptions.</span>}
      rightSlot={rightSlot}
      csvFilename="subscriptions.csv"
    />
  );
}
