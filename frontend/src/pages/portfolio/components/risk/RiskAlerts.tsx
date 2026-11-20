import { AlertTriangle, Info, ShieldCheck, XCircle } from "lucide-react";
import { Badge } from "../../../../components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "../../../../components/ui/card";
import type { RiskAlertItem } from "../../../../lib/api";

const ICON = { critical: XCircle, warning: AlertTriangle, info: Info } as const;
const TONE = { critical: "text-danger", warning: "text-warn", info: "text-info" } as const;
const BADGE = { critical: "danger", warning: "warning", info: "info" } as const;

const TYPE_LABEL: Record<string, string> = {
  drawdown: "Drawdown",
  currency: "Currency",
  concentration: "Look-through",
  concentration_single: "Single name",
  concentration_top3: "Top 3 names",
  concentration_context: "Context",
};

function severityKey(s: string): keyof typeof ICON {
  return s === "critical" || s === "warning" ? s : "info";
}

/** Drawdown, currency and single-name concentration alerts, critical first. */
export function RiskAlerts({ alerts }: { alerts: RiskAlertItem[] }) {
  const actionable = alerts.filter((a) => a.severity === "critical" || a.severity === "warning").length;
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm font-medium text-text-secondary">Risk alerts</CardTitle>
          {actionable > 0 ? <Badge variant="warning">{actionable} to look at</Badge> : null}
        </div>
      </CardHeader>
      <CardContent>
        {alerts.length === 0 ? (
          <div className="flex items-center gap-2 py-4 text-sm text-text-secondary" role="status">
            <ShieldCheck className="h-4 w-4 text-success" aria-hidden="true" />
            No alerts. Drawdown, currency and single-name concentration are inside your thresholds.
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {alerts.map((a) => {
              const key = severityKey(a.severity);
              const Icon = ICON[key];
              return (
                <li key={a.id} className="flex items-start gap-3 py-2.5">
                  <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${TONE[key]}`} aria-hidden="true" />
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-text-primary">{a.title}</span>
                      <Badge variant={BADGE[key]} className="shrink-0">
                        {a.severity}
                      </Badge>
                      <span className="text-[11px] text-text-muted">{TYPE_LABEL[a.type] ?? a.type}</span>
                    </div>
                    <p className="text-xs text-text-secondary">{a.message}</p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
