import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { InvoiceTable, type InvoiceRow } from "./components/InvoiceTable";
import { AddInvoiceDialog, type InvoiceFormData } from "./components/AddInvoiceDialog";
import { KpiTile } from "../../components/composed/KpiTile";
import { MetricGroup } from "../../components/composed/MetricGroup";
import { formatCurrency } from "../../lib/format";
import { toast } from "sonner";

export function InvoicesTab() {
  const queryClient = useQueryClient();
  const invoices = useQuery({
    queryKey: ["invoices"],
    queryFn: () => api<InvoiceRow[]>("/api/budget/invoices"),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["invoices"] });
    queryClient.invalidateQueries({ queryKey: ["budget-summary"] });
  };
  const refreshPaid = () => {
    refresh();
    queryClient.invalidateQueries({ queryKey: ["expenses"] });
    queryClient.invalidateQueries({ queryKey: ["budget-cashflow"] });
  };

  const createInvoice = useMutation({
    mutationFn: (data: InvoiceFormData) =>
      api("/api/budget/invoices", { method: "POST", body: JSON.stringify(data) }),
    onSuccess: () => { refresh(); toast.success("Invoice added"); },
    onError: (err: Error) => toast.error(err.message),
  });

  const markPaid = useMutation({
    mutationFn: (id: string) =>
      api(`/api/budget/invoices/${id}/mark-paid`, {
        method: "POST",
        body: JSON.stringify({ create_expense: true }),
      }),
    onSuccess: () => { refreshPaid(); toast.success("Marked as paid"); },
    onError: (err: Error) => toast.error(err.message),
  });

  const markUnpaid = useMutation({
    mutationFn: (id: string) =>
      api(`/api/budget/invoices/${id}/mark-unpaid`, { method: "POST" }),
    onSuccess: () => { refresh(); toast.success("Reopened"); },
    onError: (err: Error) => toast.error(err.message),
  });

  const deleteInvoice = useMutation({
    mutationFn: (id: string) =>
      api(`/api/budget/invoices/${id}`, { method: "DELETE" }),
    onSuccess: () => { refresh(); toast.success("Deleted"); },
    onError: (err: Error) => toast.error(err.message),
  });

  const openTotal = (invoices.data ?? [])
    .filter((row) => !row.paid)
    .reduce((sum, row) => sum + Number(row.amount), 0);

  const openCount = (invoices.data ?? []).filter((row) => !row.paid).length;

  return (
    <div className="space-y-6">
      <MetricGroup>
        <KpiTile label="Total Invoices" value={String(invoices.data?.length ?? 0)} />
        <KpiTile label="Open" value={String(openCount)} tone="warn" />
        <KpiTile label="Open Total" value={formatCurrency(openTotal)} tone={openCount > 0 ? "bad" : "neutral"} />
      </MetricGroup>

      <InvoiceTable
        data={invoices.data ?? []}
        onMarkPaid={(id) => markPaid.mutate(id)}
        onMarkUnpaid={(id) => markUnpaid.mutate(id)}
        onDelete={(id) => deleteInvoice.mutate(id)}
        loading={invoices.isLoading}
        rightSlot={
          <AddInvoiceDialog onSave={(data) => createInvoice.mutate(data)} />
        }
      />
    </div>
  );
}
