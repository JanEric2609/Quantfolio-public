import { Sparkles } from "lucide-react";
import { formatCurrency } from "../../../lib/format";

export function SurplusSuggestion({ amount }: { amount: number }) {
  if (amount <= 0) return null;
  return (
    <div className="flex items-start gap-3 rounded-lg border border-border bg-surface-2 p-4 text-sm">
      <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
      <p className="text-text-secondary">
        <span className="font-medium text-text-primary">You had {formatCurrency(amount)} left over this month.</span>{" "}
        This is an informational estimate, not a recommendation — nothing here moves money automatically.
      </p>
    </div>
  );
}
