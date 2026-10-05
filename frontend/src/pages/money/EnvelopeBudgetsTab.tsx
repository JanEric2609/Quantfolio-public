import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "../../lib/api";
import { formatCurrency } from "../../lib/format";
import { Card, CardContent } from "../../components/ui/card";
import { Progress } from "../../components/ui/progress";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { EnvelopeCoverSuggestion } from "./components/EnvelopeCoverSuggestion";

export interface EnvelopeStatus {
  category_id: string;
  category_name: string;
  color: string;
  icon: string;
  budgeted: number;
  spent: number;
  remaining: number;
  overspent: boolean;
  goal: { target_amount: number; target_date: string; progress: number; required_monthly: number } | null;
}

interface EnvelopesResponse {
  money_to_budget: number;
  envelopes: EnvelopeStatus[];
}

export function EnvelopeBudgetsTab() {
  const today = new Date();
  const year = today.getFullYear();
  const month = today.getMonth() + 1;
  const queryClient = useQueryClient();

  const envelopes = useQuery({
    queryKey: ["envelopes", year, month],
    queryFn: () => api<EnvelopesResponse>(`/api/budget/envelopes?year=${year}&month=${month}`),
  });

  const setBudget = useMutation({
    mutationFn: ({ categoryId, amount }: { categoryId: string; amount: number }) =>
      api(`/api/budget/envelopes/${categoryId}`, {
        method: "PUT",
        body: JSON.stringify({ year, month, budgeted_amount: amount }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["envelopes", year, month] });
      toast.success("Envelope budget updated");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const rows = envelopes.data?.envelopes ?? [];
  const moneyToBudget = envelopes.data?.money_to_budget ?? 0;
  const totalBudgeted = rows.reduce((sum, row) => sum + row.budgeted, 0);

  return (
    <div className="space-y-4">
      {!envelopes.isLoading && (
        <p className="text-sm text-text-secondary">
          {formatCurrency(totalBudgeted)} of {formatCurrency(moneyToBudget)} budgeted this month.
        </p>
      )}
      {envelopes.isLoading && <p className="text-sm text-text-muted">Loading envelopes…</p>}
      {rows.length === 0 && !envelopes.isLoading && (
        <p className="text-sm text-text-muted">No expense categories yet.</p>
      )}
      {rows.map((row) => (
        <Card key={`${row.category_id}-${year}-${month}`}>
          <CardContent className="pt-6 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="h-3 w-3 rounded-full" style={{ backgroundColor: row.color }} />
                <span className="font-medium">{row.category_name}</span>
              </div>
              <span className={row.overspent ? "text-danger text-sm font-medium" : "text-sm text-text-secondary"}>
                {formatCurrency(row.remaining)} left
              </span>
            </div>
            <Progress
              value={Math.min(row.spent, row.budgeted || row.spent)}
              max={row.budgeted || Math.max(row.spent, 1)}
            />
            <div className="flex items-center justify-between gap-2 text-sm text-text-secondary">
              <span>{formatCurrency(row.spent)} spent</span>
              <BudgetInput
                categoryName={row.category_name}
                initialValue={row.budgeted}
                onSave={(amount) => setBudget.mutate({ categoryId: row.category_id, amount })}
              />
            </div>
            {row.goal && (
              <div className="flex items-center justify-between gap-2 text-sm text-text-secondary">
                <span>
                  {formatCurrency(row.goal.progress)} / {formatCurrency(row.goal.target_amount)} goal by{" "}
                  {row.goal.target_date}
                </span>
                {row.goal.required_monthly > 0 && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 px-2"
                    onClick={() =>
                      setBudget.mutate({ categoryId: row.category_id, amount: row.goal!.required_monthly })
                    }
                  >
                    Use suggested {formatCurrency(row.goal.required_monthly)}/mo
                  </Button>
                )}
              </div>
            )}
            {row.overspent && (
              <EnvelopeCoverSuggestion
                toCategoryId={row.category_id}
                toCategoryName={row.category_name}
                deficit={Math.abs(row.remaining)}
                year={year}
                month={month}
                candidates={rows
                  .filter((candidate) => !candidate.overspent && candidate.category_id !== row.category_id)
                  .map((candidate) => ({
                    category_id: candidate.category_id,
                    category_name: candidate.category_name,
                    remaining: candidate.remaining,
                  }))}
              />
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function BudgetInput({
  categoryName,
  initialValue,
  onSave,
}: {
  categoryName: string;
  initialValue: number;
  onSave: (amount: number) => void;
}) {
  const [value, setValue] = useState(String(initialValue));
  return (
    <form
      className="flex items-center gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        const amount = Number(value);
        if (!Number.isNaN(amount) && amount >= 0) onSave(amount);
      }}
    >
      <Input
        aria-label={`Budget for ${categoryName}`}
        className="h-7 w-20 text-right"
        inputMode="decimal"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <Button type="submit" size="sm" variant="outline" className="h-11 px-2 sm:h-7">
        Set
      </Button>
    </form>
  );
}
