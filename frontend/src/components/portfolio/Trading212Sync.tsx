import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import {
  getTrading212Pies,
  getTrading212Status,
  syncTrading212,
  type Trading212State,
  type Trading212Status,
} from "../../lib/api";
import { formatCurrency, formatDateTime } from "../../lib/format";

const STATE_BADGE: Record<Trading212State, { variant: "success" | "warning" | "danger" | "secondary"; label: string }> = {
  ready: { variant: "success", label: "synced" },
  running: { variant: "secondary", label: "syncing" },
  never_synced: { variant: "secondary", label: "not synced yet" },
  not_configured: { variant: "warning", label: "no key" },
  key_invalid: { variant: "warning", label: "key refused" },
  key_scope: { variant: "warning", label: "key lacks a permission" },
  error: { variant: "danger", label: "sync failed" },
  disabled: { variant: "secondary", label: "off" },
};

export function useTrading212Status() {
  return useQuery<Trading212Status>({ queryKey: ["trading212-status"], queryFn: getTrading212Status, staleTime: 30_000 });
}

const pct = (fraction: string | number | null | undefined, digits = 1) =>
  fraction == null || fraction === "" ? "—" : `${(Number(fraction) * 100).toFixed(digits)} %`;

/** Status badge + "Sync Trading 212". Renders nothing while the connection is off. */
export function Trading212SyncButton() {
  const queryClient = useQueryClient();
  const status = useTrading212Status();
  const sync = useMutation({
    mutationFn: syncTrading212,
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
    "Read-only: Quantfolio cannot trade at Trading 212.",
  ].filter(Boolean).join(" · ");
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant={badge.variant} title={detail} aria-label={`Trading 212: ${badge.label}. ${detail}`}>
        Trading 212: {badge.label}
      </Badge>
      {data.interest_rate_measured != null && (
        <span className="text-xs text-text-secondary" title="Interest on uninvested cash paid in the last 30 days, annualised over today's cash">
          cash interest {pct(data.interest_rate_measured, 2)} p.a. (measured)
        </span>
      )}
      <Button
        type="button"
        variant="outline"
        onClick={() => sync.mutate()}
        disabled={sync.isPending || data.state === "running" || data.state === "not_configured"}
        title="Read positions, cash, history and pies from Trading 212 (read-only)"
      >
        <RefreshCw className={sync.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} aria-hidden="true" />
        Sync Trading 212
      </Button>
      {["not_configured", "key_invalid", "key_scope", "error"].includes(data.state) ? (
        <Button asChild variant="ghost" size="sm">
          <Link to="/settings/bank?connection=trading212">Fix</Link>
        </Button>
      ) : null}
    </div>
  );
}

/** Pies as Trading 212 last reported them. Read-only, and kept when a later read fails. */
export function Trading212Pies() {
  const status = useTrading212Status();
  const pies = useQuery({
    queryKey: ["trading212-pies"],
    queryFn: getTrading212Pies,
    enabled: Boolean(status.data?.enabled && status.data.pies),
  });
  if (!pies.data?.length) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Trading 212 pies
          <ShieldCheck className="h-4 w-4 text-text-muted" aria-label="Read-only" />
        </CardTitle>
        <CardDescription>
          As of {pies.data[0].synced_at ? formatDateTime(pies.data[0].synced_at) : "the last sync"}. Change them in the
          Trading 212 app; Quantfolio only reads them.
          {status.data?.pies_unavailable ? " The latest read failed, so these are older." : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {pies.data.map((pie) => (
          <section key={pie.id} aria-label={`Pie ${pie.name}`} className="rounded-md border border-border p-3">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <h3 className="font-medium text-text-primary">{pie.name}</h3>
              <div className="text-sm tabular-nums">
                {pie.value != null ? formatCurrency(pie.value) : "—"}
                {pie.result != null && (
                  <span className={pie.result >= 0 ? "ml-2 text-success" : "ml-2 text-danger"}>
                    {pie.result >= 0 ? "+" : ""}{formatCurrency(pie.result)} ({pct(pie.result_pct)})
                  </span>
                )}
              </div>
            </div>
            <div className="mt-1 text-xs text-text-muted">
              {[
                pie.cash ? `${formatCurrency(pie.cash)} cash` : null,
                pie.dividend_action === "REINVEST" ? "dividends reinvested" : pie.dividend_action ? "dividends paid out" : null,
                pie.dividends_gained ? `${formatCurrency(pie.dividends_gained)} dividends so far` : null,
                pie.goal ? `goal ${formatCurrency(pie.goal)}` : null,
                pie.status ? pie.status.replace("_", " ").toLowerCase() : null,
              ].filter(Boolean).join(" · ")}
            </div>
            {pie.instruments.length > 0 && (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-left text-xs text-text-secondary">
                    <tr className="border-b border-border">
                      <th className="py-1 pr-3 font-medium">Holding</th>
                      <th className="py-1 pr-3 text-right font-medium">Target</th>
                      <th className="py-1 pr-3 text-right font-medium">Actual</th>
                      <th className="py-1 text-right font-medium">Value</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pie.instruments.map((inst) => (
                      <tr key={inst.t212_ticker ?? inst.isin ?? inst.name} className="border-b border-border/60">
                        <td className="py-1 pr-3">
                          <div className="truncate">{inst.name || inst.t212_ticker}</div>
                          {inst.issues.length > 0 && (
                            <div className="text-xs text-warn">{inst.issues.map((i) => i.name.replaceAll("_", " ").toLowerCase()).join(", ")}</div>
                          )}
                        </td>
                        <td className="py-1 pr-3 text-right tabular-nums">{pct(inst.expected_share)}</td>
                        <td className="py-1 pr-3 text-right tabular-nums">{pct(inst.current_share)}</td>
                        <td className="py-1 text-right tabular-nums">{inst.value != null ? formatCurrency(Number(inst.value)) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        ))}
      </CardContent>
    </Card>
  );
}
