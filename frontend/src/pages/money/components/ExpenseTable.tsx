import { useMemo } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { DataTable } from "../../../components/composed/DataTable";
import { Badge } from "../../../components/ui/badge";
import { formatCurrency, formatDate } from "../../../lib/format";

export interface ExpenseRow {
  id: string;
  date: string;
  amount: string;
  description: string;
  currency: string;
  source: string;
  category_id?: string;
  notes?: string;
  account_name?: string | null;
}

export function ExpenseTable({ data, categories = {}, onRowClick, rightSlot, toolbar, loading }: {
  data: ExpenseRow[];
  categories?: Record<string, string>;
  onRowClick?: (row: ExpenseRow) => void;
  rightSlot?: React.ReactNode;
  loading?: boolean;
  toolbar?: React.ReactNode;
}) {
  const columns: ColumnDef<ExpenseRow>[] = useMemo(() => [
    { accessorKey: "date", header: "Date", cell: (c) => formatDate(c.getValue() as string) },
    { accessorKey: "category_id", header: "Category", cell: (c) => <Badge variant="secondary">{categories[c.getValue() as string] ?? "Uncategorized"}</Badge> },
    { accessorKey: "amount", header: "Amount", cell: (c) => {
      const row = c.row.original;
      return formatCurrency(Number(row.amount), { currency: row.currency });
    }},
    { accessorKey: "description", header: "Description" },
    { accessorKey: "source", header: "Source" },
    { accessorKey: "account_name", header: "Account", cell: (c) => c.getValue() as string ?? "—" },
  ], [categories]);

  if (loading) {
    return <div className="text-sm text-text-muted py-8 text-center">Loading expenses…</div>;
  }

  return (
    <DataTable
      data={data}
      columns={columns}
      onRowClick={onRowClick}
      enableFilter={false}
      emptyState={<span className="text-text-muted">No expenses yet.</span>}
      rightSlot={<>{toolbar}{rightSlot}</>}
      csvFilename="expenses.csv"
    />
  );
}
