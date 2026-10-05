import { useMemo } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { DataTable } from "../../../components/composed/DataTable";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "../../../components/ui/alert-dialog";
import { formatCurrency, formatDate } from "../../../lib/format";

export interface InvoiceRow {
  id: string;
  issuer: string;
  amount: string;
  currency: string;
  due_date?: string;
  paid: boolean;
  notes?: string;
}

export function InvoiceTable({ data, onMarkPaid, onMarkUnpaid, onDelete, rightSlot, loading }: {
  data: InvoiceRow[];
  onMarkPaid?: (id: string) => void;
  onMarkUnpaid?: (id: string) => void;
  onDelete?: (id: string) => void;
  rightSlot?: React.ReactNode;
  loading?: boolean;
}) {
  const columns: ColumnDef<InvoiceRow>[] = useMemo(() => [
    { accessorKey: "issuer", header: "Issuer" },
    { accessorKey: "amount", header: "Amount", cell: (c) => {
      const row = c.row.original;
      return formatCurrency(Number(row.amount), { currency: row.currency });
    }},
    { accessorKey: "due_date", header: "Due", cell: (c) => formatDate(c.getValue() as string) },
    {
      id: "paid",
      header: "Status",
      cell: (c) => c.row.original.paid
        ? <Badge variant="success">Paid</Badge>
        : <Badge variant="warning">Open</Badge>,
    },
    {
      id: "days_overdue",
      header: "Overdue",
      cell: (c) => {
        const row = c.row.original;
        if (row.paid || !row.due_date) return <span className="text-text-muted">—</span>;
        const days = Math.ceil((Date.now() - new Date(row.due_date).getTime()) / (1000 * 60 * 60 * 24));
        if (days <= 0) return <span className="text-text-muted">—</span>;
        return <Badge variant="danger">{days}d</Badge>;
      },
    },
    {
      id: "actions",
      header: "",
      cell: (c) => (
        <div className="flex gap-1">
          {c.row.original.paid
            ? onMarkUnpaid && <Button variant="ghost" size="sm" onClick={() => onMarkUnpaid(c.row.original.id)}>Reopen</Button>
            : onMarkPaid && <Button variant="ghost" size="sm" onClick={() => onMarkPaid(c.row.original.id)}>Paid</Button>
          }
          {onDelete && <AlertDialog><AlertDialogTrigger asChild><Button variant="ghost" size="sm">Delete</Button></AlertDialogTrigger><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Delete invoice?</AlertDialogTitle><AlertDialogDescription>This removes the invoice record.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={() => onDelete(c.row.original.id)}>Delete</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>}
        </div>
      ),
    },
  ], [onMarkPaid, onMarkUnpaid, onDelete]);

  if (loading) {
    return <div className="text-sm text-text-muted py-8 text-center">Loading invoices…</div>;
  }

  return (
    <DataTable
      data={data}
      columns={columns}
      enableFilter
      emptyState={<span className="text-text-muted">No invoices.</span>}
      rightSlot={rightSlot}
      csvFilename="invoices.csv"
    />
  );
}
