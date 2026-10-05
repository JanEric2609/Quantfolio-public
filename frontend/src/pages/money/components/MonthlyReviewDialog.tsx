import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "../../../lib/api";
import { formatCurrency } from "../../../lib/format";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../../../components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../../components/ui/tabs";
import { CategoryChips } from "./CategoryChips";
import type { EnvelopeStatus } from "../EnvelopeBudgetsTab";

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

interface ReviewSummary {
  uncategorized_expenses: { id: string; date: string; amount: number; currency: string; description: string; source: string; notes: string | null }[];
  potential_duplicates: {
    manual_expense_id: string;
    manual_description: string;
    manual_date: string;
    synced_expense_id: string;
    synced_description: string;
    synced_date: string;
    amount: number;
  }[];
  sinking_funds: { category_id: string; category_name: string; target_amount: number; target_date: string; progress: number; required_monthly: number }[];
}

interface Category { id: string; name: string; color: string; icon: string; type: string }

export function MonthlyReviewDialog({
  open,
  onOpenChange,
  reviewYear,
  reviewMonth,
  targetYear,
  targetMonth,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  reviewYear: number;
  reviewMonth: number;
  targetYear: number;
  targetMonth: number;
}) {
  const queryClient = useQueryClient();

  const review = useQuery({
    queryKey: ["budget-review", reviewYear, reviewMonth],
    queryFn: () => api<ReviewSummary>(`/api/budget/review?year=${reviewYear}&month=${reviewMonth}`),
    enabled: open,
  });
  const priorEnvelopes = useQuery({
    queryKey: ["envelopes", reviewYear, reviewMonth],
    queryFn: () => api<{ envelopes: EnvelopeStatus[] }>(`/api/budget/envelopes?year=${reviewYear}&month=${reviewMonth}`),
    enabled: open,
  });
  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => api<Category[]>("/api/budget/categories"),
    enabled: open,
  });

  const invalidateReview = () => queryClient.invalidateQueries({ queryKey: ["budget-review", reviewYear, reviewMonth] });

  const setNextMonthBudget = useMutation({
    mutationFn: ({ categoryId, amount }: { categoryId: string; amount: number }) =>
      api(`/api/budget/envelopes/${categoryId}`, {
        method: "PUT",
        body: JSON.stringify({ year: targetYear, month: targetMonth, budgeted_amount: amount }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["envelopes", targetYear, targetMonth] });
      toast.success(`${MONTH_NAMES[targetMonth - 1]} target updated`);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const recategorize = useMutation({
    mutationFn: ({ expense, categoryId }: { expense: ReviewSummary["uncategorized_expenses"][number]; categoryId: string }) =>
      api(`/api/budget/expenses/${expense.id}`, {
        method: "PUT",
        body: JSON.stringify({
          date: expense.date,
          amount: expense.amount,
          currency: expense.currency,
          description: expense.description,
          category_id: categoryId,
          source: expense.source,
          notes: expense.notes,
        }),
      }),
    onSuccess: () => {
      invalidateReview();
      queryClient.invalidateQueries({ queryKey: ["expenses"] });
      toast.success("Expense categorized");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const keepManualDeleteSynced = useMutation({
    mutationFn: (expenseId: string) => api(`/api/budget/expenses/${expenseId}`, { method: "DELETE" }),
    onSuccess: () => {
      invalidateReview();
      queryClient.invalidateQueries({ queryKey: ["expenses"] });
      toast.success("Duplicate removed");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const expenseCategories = (categories.data ?? []).filter((c) => c.type === "expense");
  const monthLabel = MONTH_NAMES[reviewMonth - 1];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Review {monthLabel}</DialogTitle>
        </DialogHeader>
        <Tabs defaultValue="targets">
          <TabsList>
            <TabsTrigger value="targets">Targets</TabsTrigger>
            <TabsTrigger value="recategorize">
              Recategorize{review.data && review.data.uncategorized_expenses.length > 0 ? ` (${review.data.uncategorized_expenses.length})` : ""}
            </TabsTrigger>
            <TabsTrigger value="reconcile">
              Reconcile{review.data && review.data.potential_duplicates.length > 0 ? ` (${review.data.potential_duplicates.length})` : ""}
            </TabsTrigger>
            <TabsTrigger value="sinking">Sinking funds</TabsTrigger>
          </TabsList>

          <TabsContent value="targets" className="space-y-2 max-h-96 overflow-y-auto">
            <p className="text-xs text-text-muted">
              How {monthLabel} went — adjust {MONTH_NAMES[targetMonth - 1]}'s targets below.
            </p>
            {(priorEnvelopes.data?.envelopes ?? []).map((row) => (
              <TargetRow
                key={row.category_id}
                row={row}
                onSave={(amount) => setNextMonthBudget.mutate({ categoryId: row.category_id, amount })}
              />
            ))}
            {priorEnvelopes.data && priorEnvelopes.data.envelopes.length === 0 && (
              <p className="text-sm text-text-muted">No envelopes to review.</p>
            )}
          </TabsContent>

          <TabsContent value="recategorize" className="space-y-3 max-h-96 overflow-y-auto">
            {(review.data?.uncategorized_expenses ?? []).map((expense) => (
              <div key={expense.id} className="space-y-2 rounded-md border border-border p-3">
                <div className="flex items-center justify-between text-sm">
                  <span>{expense.description}</span>
                  <span className="font-medium tabular-nums">{formatCurrency(Math.abs(expense.amount))}</span>
                </div>
                <CategoryChips
                  categories={expenseCategories}
                  disabled={recategorize.isPending}
                  onSelect={(categoryId) => recategorize.mutate({ expense, categoryId })}
                />
              </div>
            ))}
            {review.data && review.data.uncategorized_expenses.length === 0 && (
              <p className="text-sm text-text-muted">Nothing uncategorized this month.</p>
            )}
          </TabsContent>

          <TabsContent value="reconcile" className="space-y-2 max-h-96 overflow-y-auto">
            {(review.data?.potential_duplicates ?? []).map((pair) => (
              <div key={pair.manual_expense_id} className="space-y-2 rounded-md border border-border p-3 text-sm">
                <p>
                  <span className="font-medium">{pair.manual_description}</span> ({pair.manual_date}, manual) looks like the
                  same transaction as <span className="font-medium">{pair.synced_description}</span> ({pair.synced_date}, DKB
                  sync) — both {formatCurrency(pair.amount)}.
                </p>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={keepManualDeleteSynced.isPending}
                    onClick={() => keepManualDeleteSynced.mutate(pair.synced_expense_id)}
                  >
                    Keep manual, remove synced
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={keepManualDeleteSynced.isPending}
                    onClick={() => keepManualDeleteSynced.mutate(pair.manual_expense_id)}
                  >
                    Keep synced, remove manual
                  </Button>
                </div>
              </div>
            ))}
            {review.data && review.data.potential_duplicates.length === 0 && (
              <p className="text-sm text-text-muted">No likely duplicates found.</p>
            )}
          </TabsContent>

          <TabsContent value="sinking" className="space-y-2 max-h-96 overflow-y-auto">
            {(review.data?.sinking_funds ?? []).map((fund) => (
              <div key={fund.category_id} className="flex items-center justify-between rounded-md border border-border p-3 text-sm">
                <span>{fund.category_name}</span>
                <span className="text-text-secondary">
                  {formatCurrency(fund.progress)} / {formatCurrency(fund.target_amount)} by {fund.target_date}
                </span>
              </div>
            ))}
            {review.data && review.data.sinking_funds.length === 0 && (
              <p className="text-sm text-text-muted">No sinking funds set up yet.</p>
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

function TargetRow({ row, onSave }: { row: EnvelopeStatus; onSave: (amount: number) => void }) {
  const [value, setValue] = useState(String(row.budgeted));
  return (
    <form
      className="flex items-center justify-between gap-2 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        const amount = Number(value);
        if (!Number.isNaN(amount) && amount >= 0) onSave(amount);
      }}
    >
      <div className="flex items-center gap-2">
        <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: row.color }} />
        <span>{row.category_name}</span>
        <span className="text-xs text-text-muted">
          ({formatCurrency(row.spent)} spent of {formatCurrency(row.budgeted)})
        </span>
      </div>
      <div className="flex items-center gap-1">
        <Input
          aria-label={`Next month's target for ${row.category_name}`}
          className="h-7 w-20 text-right"
          inputMode="decimal"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <Button type="submit" size="sm" variant="outline" className="h-11 px-2 sm:h-7">
          Set
        </Button>
      </div>
    </form>
  );
}
