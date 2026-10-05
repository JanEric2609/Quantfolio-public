import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { ToggleGroup, ToggleGroupItem } from "../ui/toggle-group";
import {
  applyScalableReconcile,
  getScalableSavingsPlans,
  getScalableStatus,
  previewScalableReconcile,
  syncScalable,
  type ScalableDecision,
  type ScalableState,
  type ScalableStatus,
} from "../../lib/api";
import { formatCurrency, formatDateTime } from "../../lib/format";

const STATE_BADGE: Record<ScalableState, { variant: "success" | "warning" | "danger" | "secondary"; label: string }> = {
  ready: { variant: "success", label: "synced" },
  running: { variant: "secondary", label: "syncing" },
  never_synced: { variant: "secondary", label: "not synced yet" },
  login_required: { variant: "warning", label: "login expired" },
  guard_unattested: { variant: "danger", label: "trade guard missing" },
  not_installed: { variant: "warning", label: "not set up" },
  error: { variant: "danger", label: "sync failed" },
  disabled: { variant: "secondary", label: "off" },
};

export function useScalableStatus() {
  return useQuery<ScalableStatus>({ queryKey: ["scalable-status"], queryFn: getScalableStatus, staleTime: 30_000 });
}

/** Status badge + "Sync Scalable". Renders nothing while the connection is off. */
export function ScalableSyncButton() {
  const queryClient = useQueryClient();
  const status = useScalableStatus();
  const sync = useMutation({
    mutationFn: syncScalable,
    onSuccess: (log) => {
      if (log.state === "warning") toast.warning(log.message);
      else toast.success(log.message);
    },
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => queryClient.invalidateQueries(),
  });
  const data = status.data;
  if (!data?.enabled) return null;
  const badge = data.stale && data.state === "ready" ? { variant: "warning" as const, label: "out of date" } : STATE_BADGE[data.state];
  const detail = [
    data.last_success_at ? `Last successful sync ${formatDateTime(data.last_success_at)}` : "No successful sync yet",
    data.message,
    "Read-only: Quantfolio cannot trade at Scalable.",
  ].filter(Boolean).join(" · ");
  return (
    <div className="flex items-center gap-2">
      <Badge variant={badge.variant} title={detail} aria-label={`Scalable: ${badge.label}. ${detail}`}>
        Scalable: {badge.label}
      </Badge>
      <Button
        type="button"
        variant="outline"
        onClick={() => sync.mutate()}
        disabled={sync.isPending || data.state === "running"}
        title="Read holdings, cash and transactions from Scalable Capital (read-only)"
      >
        <RefreshCw className={sync.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} aria-hidden="true" />
        Sync Scalable
      </Button>
      {["login_required", "guard_unattested", "not_installed", "error"].includes(data.state) ? (
        <Button asChild variant="ghost" size="sm">
          <Link to="/settings/bank?connection=scalable">Fix</Link>
        </Button>
      ) : null}
    </div>
  );
}

/**
 * Hand-entered holdings that match a synced Scalable position. The GET is a dry
 * run; nothing changes until the owner confirms a decision per row.
 */
export function ScalableReview() {
  const queryClient = useQueryClient();
  const status = useScalableStatus();
  const rows = useQuery({
    queryKey: ["scalable-reconcile"],
    queryFn: previewScalableReconcile,
    enabled: Boolean(status.data?.positions),
  });
  const [choices, setChoices] = useState<Record<string, ScalableDecision>>({});
  const apply = useMutation({
    mutationFn: () => applyScalableReconcile(choices),
    onSuccess: (result) => {
      toast.success(`Replaced ${result.replaced}, kept ${result.kept}`);
      setChoices({});
    },
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => queryClient.invalidateQueries(),
  });

  const open = (rows.data ?? []).filter((row) => row.decision !== "keep_both");
  useEffect(() => {
    if (open.length && window.location.hash === "#scalable-review") {
      document.getElementById("scalable-review")?.scrollIntoView({ block: "start" });
    }
  }, [open.length]);
  if (!open.length) return null;

  const chosen = Object.keys(choices).length;
  return (
    <Card id="scalable-review" className="scroll-mt-20 border-warn/40">
      <CardHeader>
        <CardTitle>Scalable positions you also entered by hand</CardTitle>
        <CardDescription>
          These Scalable positions are not counted yet, so nothing is counted twice. Choose per holding:
          <strong className="font-medium text-text-primary"> Replace</strong> deletes your hand-entered row and counts the synced one;
          <strong className="font-medium text-text-primary"> Keep both</strong> counts both (for example the same ETF at another broker).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <ul className="divide-y divide-border rounded-md border border-border">
          {open.map((row) => (
            <li key={row.holding_id} className="flex flex-col gap-3 p-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="truncate font-medium">{row.name}</div>
                <div className="text-xs text-text-muted">
                  <span className="font-mono">{row.isin}</span>
                  {" · "}by hand {row.manual_quantity} ({formatCurrency(Number(row.manual_value))})
                  {" · "}Scalable {row.scalable_quantity}
                  {row.scalable_value != null ? ` (${formatCurrency(Number(row.scalable_value))})` : ""}
                  {row.quantity_matches ? " · same quantity" : " · quantities differ"}
                </div>
              </div>
              <ToggleGroup
                type="single"
                value={choices[row.holding_id] ?? ""}
                onValueChange={(value) =>
                  setChoices((current) => {
                    const next = { ...current };
                    if (value) next[row.holding_id] = value as ScalableDecision;
                    else delete next[row.holding_id];
                    return next;
                  })
                }
                aria-label={`Decision for ${row.name}`}
              >
                <ToggleGroupItem value="replace" size="sm">Replace</ToggleGroupItem>
                <ToggleGroupItem value="keep_both" size="sm">Keep both</ToggleGroupItem>
              </ToggleGroup>
            </li>
          ))}
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

/** Savings plans as Scalable reports them. Read-only: they are changed in the Scalable app. */
export function ScalableSavingsPlans() {
  const status = useScalableStatus();
  const plans = useQuery({
    queryKey: ["scalable-savings-plans"],
    queryFn: getScalableSavingsPlans,
    enabled: Boolean(status.data?.enabled && status.data.last_success_at),
  });
  if (!plans.data?.length) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Scalable savings plans
          <ShieldCheck className="h-4 w-4 text-text-muted" aria-label="Read-only" />
        </CardTitle>
        <CardDescription>As of the last sync. Change them in the Scalable app; Quantfolio only reads them.</CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-border">
          {plans.data.map((plan, index) => (
            <li key={`${plan.isin ?? plan.name}-${index}`} className="flex items-center justify-between gap-3 py-2 text-sm">
              <div className="min-w-0">
                <div className="truncate">{plan.name || plan.isin}</div>
                {plan.isin ? <div className="font-mono text-xs text-text-muted">{plan.isin}</div> : null}
              </div>
              <div className="shrink-0 text-right">
                <div className="font-mono tabular-nums">{plan.amount != null ? formatCurrency(Number(plan.amount)) : "—"}</div>
                <div className="text-xs text-text-muted">
                  {[plan.frequency?.toLowerCase(), plan.day_of_month ? `day ${plan.day_of_month}` : null].filter(Boolean).join(", ")}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
