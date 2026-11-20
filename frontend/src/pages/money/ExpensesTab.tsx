import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { ExpenseTable, type ExpenseRow } from "./components/ExpenseTable";
import { AddExpenseDialog, type ExpenseFormData } from "./components/AddExpenseDialog";
import { CategoryManager } from "./components/CategoryManager";
import { toast } from "sonner";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Input } from "../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { ExpenseDetailSheet } from "./components/ExpenseDetailSheet";

interface Category {
  id: string;
  name: string;
  color: string;
  icon: string;
  type: "income" | "expense" | "investment";
}

export function ExpensesTab() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<ExpenseRow | null>(null);
  const expenses = useQuery({
    queryKey: ["expenses"],
    queryFn: () => api<ExpenseRow[]>("/api/budget/expenses"),
  });

  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => api<Category[]>("/api/budget/categories"),
  });

  const createExpense = useMutation({
    mutationFn: (data: ExpenseFormData) =>
      api("/api/budget/expenses", { method: "POST", body: JSON.stringify(data) }),
    onSuccess: () => {
      refresh();
      toast.success("Expense added");
    },
    onError: (err: Error) => toast.error(err.message),
  });
  const updateExpense = useMutation({
    mutationFn: (data: ExpenseFormData) => api(`/api/budget/expenses/${selected?.id}`, { method: "PUT", body: JSON.stringify(data) }),
    onSuccess: () => { refresh(); setSelected(null); toast.success("Expense updated"); },
    onError: (err: Error) => toast.error(err.message),
  });
  const deleteExpense = useMutation({
    mutationFn: () => api(`/api/budget/expenses/${selected?.id}`, { method: "DELETE" }),
    onSuccess: () => { refresh(); setSelected(null); toast.success("Expense deleted"); },
    onError: (err: Error) => toast.error(err.message),
  });
  const refresh = () => {
    ["expenses", "budget-summary", "budget-cashflow"].forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
  };

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    setParams(next);
  };
  const filtered = useMemo(() => (expenses.data ?? []).filter((row) => {
    const search = params.get("search")?.toLowerCase();
    const category = params.get("category");
    const from = params.get("from");
    const to = params.get("to");
    return (!search || row.description.toLowerCase().includes(search))
      && (!category || row.category_id === category)
      && (!from || row.date >= from)
      && (!to || row.date <= to);
  }), [expenses.data, params]);
  const categoryNames = Object.fromEntries((categories.data ?? []).map((row) => [row.id, row.name]));

  return (
    <div className="space-y-4">
      <ExpenseTable
        data={filtered}
        categories={categoryNames}
        onRowClick={setSelected}
        loading={expenses.isLoading}
        rightSlot={
          <div className="flex gap-2">
            <CategoryManager categories={categories.data ?? []} />
            <AddExpenseDialog
              categories={(categories.data ?? []).map((c) => ({ id: c.id, name: c.name }))}
              onSave={(data) => createExpense.mutate(data)}
              pending={createExpense.isPending}
            />
          </div>
        }
        toolbar={<div className="flex flex-wrap gap-2">
          <Input aria-label="Search expenses" placeholder="Search description" value={params.get("search") ?? ""} onChange={(e) => setFilter("search", e.target.value)} className="w-52" />
          <Select value={params.get("category") ?? "all"} onValueChange={(value) => setFilter("category", value === "all" ? "" : value)}><SelectTrigger className="w-44"><SelectValue placeholder="Category" /></SelectTrigger><SelectContent><SelectItem value="all">All categories</SelectItem>{(categories.data ?? []).map((row) => <SelectItem key={row.id} value={row.id}>{row.name}</SelectItem>)}</SelectContent></Select>
          <Input aria-label="From date" type="date" value={params.get("from") ?? ""} onChange={(e) => setFilter("from", e.target.value)} className="w-40" />
          <Input aria-label="To date" type="date" value={params.get("to") ?? ""} onChange={(e) => setFilter("to", e.target.value)} className="w-40" />
        </div>}
      />
      <ExpenseDetailSheet expense={selected} categories={(categories.data ?? []).map((c) => ({ id: c.id, name: c.name }))} pending={updateExpense.isPending} onClose={() => setSelected(null)} onSave={(data) => updateExpense.mutate(data)} onDelete={() => deleteExpense.mutate()} />
    </div>
  );
}
