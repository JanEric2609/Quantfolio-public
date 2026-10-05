import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { formatCurrency } from "../../lib/format";
import { SubscriptionTable, type SubscriptionRow } from "./components/SubscriptionTable";
import { AddSubscriptionDialog, type SubscriptionFormData } from "./components/AddSubscriptionDialog";
import { Card, CardContent } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { toast } from "sonner";

interface Category { id: string; name: string; }
interface SubscriptionSuggestion {
  name: string;
  amount: number;
  currency: string;
  billing_cycle: string;
  next_due_date: string;
  source: string;
  occurrences: number;
}

export function SubscriptionsTab() {
  const queryClient = useQueryClient();
  const [suggestion, setSuggestion] = useState<Partial<SubscriptionFormData>>();
  const [addOpen, setAddOpen] = useState(false);
  const subscriptions = useQuery({ queryKey: ["subscriptions"], queryFn: () => api<SubscriptionRow[]>("/api/budget/subscriptions") });
  const categories = useQuery({ queryKey: ["categories"], queryFn: () => api<Category[]>("/api/budget/categories") });
  const suggestions = useQuery({ queryKey: ["subscription-suggestions"], queryFn: () => api<SubscriptionSuggestion[]>("/api/budget/subscriptions/suggestions") });
  const refresh = () => {
    ["subscriptions", "budget-summary", "subscription-suggestions"].forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
  };
  const paidRefresh = () => {
    refresh();
    ["expenses", "budget-cashflow"].forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
  };
  const create = useMutation({ mutationFn: (data: SubscriptionFormData) => api("/api/budget/subscriptions", { method: "POST", body: JSON.stringify(data) }), onSuccess: () => { refresh(); toast.success("Subscription added"); }, onError: (e: Error) => toast.error(e.message) });
  const pay = useMutation({ mutationFn: (id: string) => api(`/api/budget/subscriptions/${id}/mark-paid`, { method: "POST", body: JSON.stringify({ create_expense: true }) }), onSuccess: () => { paidRefresh(); toast.success("Marked as paid"); }, onError: (e: Error) => toast.error(e.message) });
  const toggle = useMutation({ mutationFn: (row: SubscriptionRow) => api(`/api/budget/subscriptions/${row.id}`, { method: "PUT", body: JSON.stringify({ ...row, active: !row.active }) }), onSuccess: () => { refresh(); toast.success("Updated"); }, onError: (e: Error) => toast.error(e.message) });
  const remove = useMutation({ mutationFn: (id: string) => api(`/api/budget/subscriptions/${id}`, { method: "DELETE" }), onSuccess: () => { refresh(); toast.success("Deleted"); }, onError: (e: Error) => toast.error(e.message) });
  return <div className="space-y-6">
    {suggestions.data?.length ? <Card><CardContent className="pt-6"><h3 className="mb-3 text-sm font-medium text-text-secondary">Suggestions</h3><div className="grid grid-cols-1 gap-3 sm:grid-cols-2">{suggestions.data.map((item, i) =>
      <button key={`${item.source}-${item.name}-${i}`} type="button" className="rounded-md border border-border bg-surface-2 p-3 text-left text-sm transition-colors hover:bg-surface-3" onClick={() => { setSuggestion({ ...item, currency: item.currency as SubscriptionFormData["currency"], billing_cycle: item.billing_cycle as SubscriptionFormData["billing_cycle"] }); setAddOpen(true); }}>
        <span className="block font-medium">{item.name}</span><span className="mt-1 block text-xs text-text-muted">{formatCurrency(item.amount, { currency: item.currency })} / {item.billing_cycle} / {item.source}</span><Badge variant="secondary" className="mt-1 text-xs">{item.occurrences}x</Badge>
      </button>)}</div></CardContent></Card> : null}
    <SubscriptionTable data={subscriptions.data ?? []} categories={Object.fromEntries((categories.data ?? []).map((c) => [c.id, c.name]))} onMarkPaid={(id) => pay.mutate(id)} onToggleActive={(row) => toggle.mutate(row)} onDelete={(id) => remove.mutate(id)} loading={subscriptions.isLoading} rightSlot={<AddSubscriptionDialog categories={categories.data ?? []} onSave={(data) => create.mutate(data)} pending={create.isPending} open={addOpen} onOpenChange={setAddOpen} initialValues={suggestion} />} />
  </div>;
}
