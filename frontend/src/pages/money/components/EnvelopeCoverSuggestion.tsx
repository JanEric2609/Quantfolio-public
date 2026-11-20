import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { X } from "lucide-react";
import { api } from "../../../lib/api";
import { formatCurrency } from "../../../lib/format";
import { Button } from "../../../components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";

export interface CoverCandidate {
  category_id: string;
  category_name: string;
  remaining: number;
}

export function EnvelopeCoverSuggestion({
  toCategoryId,
  toCategoryName,
  deficit,
  year,
  month,
  candidates,
}: {
  toCategoryId: string;
  toCategoryName: string;
  deficit: number;
  year: number;
  month: number;
  candidates: CoverCandidate[];
}) {
  const [dismissed, setDismissed] = useState(false);
  const [fromCategoryId, setFromCategoryId] = useState(candidates[0]?.category_id ?? "");
  const queryClient = useQueryClient();

  const cover = useMutation({
    mutationFn: () =>
      api("/api/budget/envelopes/cover", {
        method: "POST",
        body: JSON.stringify({
          from_category_id: fromCategoryId,
          to_category_id: toCategoryId,
          amount: deficit,
          year,
          month,
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["envelopes", year, month] });
      toast.success(`Moved ${formatCurrency(deficit)} to ${toCategoryName}`);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (dismissed || candidates.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-danger/30 bg-danger/5 p-2 text-sm">
      <span>Move {formatCurrency(deficit)} from</span>
      <Select value={fromCategoryId} onValueChange={setFromCategoryId}>
        <SelectTrigger className="h-7 w-36" aria-label="Cover from category">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {candidates.map((c) => (
            <SelectItem key={c.category_id} value={c.category_id}>
              {c.category_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <span>to cover {toCategoryName}?</span>
      <Button
        size="sm"
        variant="outline"
        className="h-7"
        disabled={cover.isPending || !fromCategoryId}
        onClick={() => cover.mutate()}
      >
        Move
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className="h-11 w-11 p-0 sm:h-7 sm:w-7"
        aria-label="Dismiss suggestion"
        onClick={() => setDismissed(true)}
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  );
}
