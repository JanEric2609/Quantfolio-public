import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus } from "lucide-react";
import { api } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "../../../components/ui/sheet";
import { Numpad, formatRawAmount, rawToAmount } from "./Numpad";
import { CategoryChips, type CategoryChipItem } from "./CategoryChips";
import { deriveFavorites, type FavoriteExpense } from "./favorites";
import { type ExpenseRow } from "./ExpenseTable";
import { formatCurrency } from "../../../lib/format";

interface Category extends CategoryChipItem {
  type: "income" | "expense" | "investment";
}

export function QuickAddExpense() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState("");

  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => api<Category[]>("/api/budget/categories"),
  });
  const expenses = useQuery({
    queryKey: ["expenses"],
    queryFn: () => api<ExpenseRow[]>("/api/budget/expenses"),
    enabled: open,
  });

  const expenseCategories = useMemo(
    () => (categories.data ?? []).filter((category) => category.type === "expense"),
    [categories.data]
  );
  const incomeCategoryIds = useMemo(
    () => new Set((categories.data ?? []).filter((category) => category.type === "income").map((category) => category.id)),
    [categories.data]
  );
  const favorites = useMemo(
    () =>
      deriveFavorites(
        (expenses.data ?? []).filter((expense) => !incomeCategoryIds.has(expense.category_id ?? ""))
      ),
    [expenses.data, incomeCategoryIds]
  );

  const refresh = () => {
    ["expenses", "budget-summary", "budget-cashflow"].forEach((key) =>
      queryClient.invalidateQueries({ queryKey: [key] })
    );
  };

  const createExpense = useMutation({
    mutationFn: (payload: { amount: number; description: string; category_id: string | null }) =>
      api("/api/budget/expenses", {
        method: "POST",
        body: JSON.stringify({
          date: new Date().toISOString().slice(0, 10),
          amount: payload.amount,
          currency: "EUR",
          description: payload.description,
          category_id: payload.category_id,
        }),
      }),
    onSuccess: () => {
      refresh();
      toast.success("Expense added");
      setRaw("");
      setOpen(false);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const handleCategorySelect = (categoryId: string) => {
    const amount = rawToAmount(raw);
    if (amount <= 0) {
      toast.error("Enter an amount first");
      return;
    }
    const category = expenseCategories.find((c) => c.id === categoryId);
    createExpense.mutate({ amount, description: category?.name ?? "Expense", category_id: categoryId });
  };

  const handleFavorite = (favorite: FavoriteExpense) => {
    createExpense.mutate({
      amount: favorite.amount,
      description: favorite.description,
      category_id: favorite.category_id,
    });
  };

  return (
    <Sheet
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setRaw("");
      }}
    >
      <SheetTrigger asChild>
        <Button size="sm">
          <Plus className="h-4 w-4 mr-1" /> Quick Add
        </Button>
      </SheetTrigger>
      <SheetContent side="bottom" className="mx-auto w-full max-w-md space-y-4">
        <SheetHeader>
          <SheetTitle>Log an expense</SheetTitle>
        </SheetHeader>
        <div className="text-center text-4xl font-semibold tabular-nums" aria-live="polite">
          {formatRawAmount(raw)}
        </div>
        <Numpad value={raw} onChange={setRaw} />
        {favorites.length > 0 && (
          <div className="flex gap-2 overflow-x-auto pb-1" role="group" aria-label="Quick repeat">
            {favorites.map((favorite) => (
              <button
                key={favorite.key}
                type="button"
                disabled={createExpense.isPending}
                onClick={() => handleFavorite(favorite)}
                className="shrink-0 rounded-full border border-border bg-surface-2 px-3 py-1.5 text-xs text-text-secondary transition-colors hover:border-accent disabled:opacity-40"
              >
                {favorite.description} · {formatCurrency(favorite.amount, "EUR")}
              </button>
            ))}
          </div>
        )}
        <CategoryChips
          categories={expenseCategories}
          disabled={createExpense.isPending}
          onSelect={handleCategorySelect}
        />
      </SheetContent>
    </Sheet>
  );
}
