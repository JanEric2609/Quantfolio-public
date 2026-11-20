import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { ToggleGroup, ToggleGroupItem } from "../ui/toggle-group";
import { applyBrokerReconcile, previewBrokerReconcile, type ReconcileBroker, type ScalableDecision } from "../../lib/api";
import { formatCurrency } from "../../lib/format";
import { BROKER_SHORT } from "./BrokerFilter";

/**
 * Synced positions at one broker that are also entered by hand. They are held
 * back until the owner decides, so nothing counts twice. Renders nothing when
 * there is nothing to decide. `enabled` is false until the broker holds positions.
 */
export function BrokerReview({ source, enabled }: { source: ReconcileBroker; enabled: boolean }) {
  const queryClient = useQueryClient();
  const label = BROKER_SHORT[source];
  const anchor = `${source}-review`;
  const rows = useQuery({ queryKey: [`${source}-reconcile`], queryFn: () => previewBrokerReconcile(source), enabled });
  const [choices, setChoices] = useState<Record<string, ScalableDecision>>({});
  const apply = useMutation({
    mutationFn: () => applyBrokerReconcile(source, choices),
    onSuccess: (result) => {
      toast.success(`Replaced ${result.replaced}, kept ${result.kept}`);
      setChoices({});
    },
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => queryClient.invalidateQueries(),
  });

  const open = (rows.data ?? []).filter((row) => row.decision !== "keep_both");
  useEffect(() => {
    if (open.length && window.location.hash === `#${anchor}`) {
      document.getElementById(anchor)?.scrollIntoView({ block: "start" });
    }
  }, [open.length, anchor]);
  if (!open.length) return null;

  const chosen = Object.keys(choices).length;
  return (
    <Card id={anchor} className="scroll-mt-20 border-warn/40">
      <CardHeader>
        <CardTitle>{label} positions you also entered by hand</CardTitle>
        <CardDescription>
          These {label} positions are not counted yet, so nothing is counted twice. Choose per holding:
          <strong className="font-medium text-text-primary"> Replace</strong> deletes your hand-entered row and counts the synced one;
          <strong className="font-medium text-text-primary"> Keep both</strong> counts both (for example the same ETF at another broker).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <ul className="divide-y divide-border rounded-md border border-border">
          {open.map((row) => {
            const quantity = row.broker_quantity ?? row.scalable_quantity;
            const value = row.broker_value !== undefined ? row.broker_value : row.scalable_value;
            return (
              <li key={row.holding_id} className="flex flex-col gap-3 p-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="truncate font-medium">{row.name}</div>
                  <div className="text-xs text-text-muted">
                    <span className="font-mono">{row.isin}</span>
                    {" · "}by hand {row.manual_quantity} ({formatCurrency(Number(row.manual_value))})
                    {" · "}{label} {quantity}
                    {value != null ? ` (${formatCurrency(Number(value))})` : ""}
                    {row.quantity_matches ? " · same quantity" : " · quantities differ"}
                  </div>
                </div>
                <ToggleGroup
                  type="single"
                  value={choices[row.holding_id] ?? ""}
                  onValueChange={(next) =>
                    setChoices((current) => {
                      const updated = { ...current };
                      if (next) updated[row.holding_id] = next as ScalableDecision;
                      else delete updated[row.holding_id];
                      return updated;
                    })
                  }
                  aria-label={`Decision for ${row.name}`}
                >
                  <ToggleGroupItem value="replace" size="sm">Replace</ToggleGroupItem>
                  <ToggleGroupItem value="keep_both" size="sm">Keep both</ToggleGroupItem>
                </ToggleGroup>
              </li>
            );
          })}
        </ul>
        <div className="flex items-center justify-end gap-2">
          <span className="text-xs text-text-muted">{chosen} of {open.length} decided</span>
          <Button type="button" onClick={() => apply.mutate()} disabled={!chosen || apply.isPending}>
            Apply {chosen || ""} decision{chosen === 1 ? "" : "s"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
