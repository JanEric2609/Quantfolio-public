import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { FlaskConical, Rocket, History, ExternalLink, AlertCircle, TrendingUp } from "lucide-react";
import { getRecentRuns, type RecentRun } from "../../../lib/api";
import { formatDateTime, formatNumber } from "../../../lib/format";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Skeleton } from "../../../components/ui/skeleton";
import { EmptyState } from "../../../components/shared/EmptyState";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "../../../components/ui/sheet";

const TYPE_META: Record<
  RecentRun["type"],
  { label: string; icon: typeof FlaskConical; badge: "accent" | "info"; route: string }
> = {
  experiment: { label: "Experiment", icon: FlaskConical, badge: "accent", route: "/quantlab/experiments" },
  backtest: { label: "Backtest", icon: TrendingUp, badge: "info", route: "/quantlab/backtest" },
};

function statusVariant(status: string): "success" | "danger" | "warning" | "secondary" {
  const s = status.toLowerCase();
  if (["completed", "finished", "success"].includes(s)) return "success";
  if (["failed", "error", "cancelled"].includes(s)) return "danger";
  if (["running", "queued", "created", "scheduled"].includes(s)) return "warning";
  return "secondary";
}

export function RunsView() {
  const navigate = useNavigate();
  const [selected, setSelected] = useState<RecentRun | null>(null);

  const runsQuery = useQuery({
    queryKey: ["quant", "runs", "recent"],
    queryFn: () => getRecentRuns(50),
  });

  const runs = runsQuery.data ?? [];

  if (runsQuery.isLoading) {
    return (
      <div className="space-y-2 p-1">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    );
  }

  if (runsQuery.isError) {
    return (
      <EmptyState
        icon={AlertCircle}
        title="Couldn't load runs"
        description="An error occurred while fetching recent runs. Try again in a moment."
      />
    );
  }

  if (!runs.length) {
    return (
      <EmptyState
        icon={History}
        title="No runs yet"
        description="Run a quant experiment or a strategy backtest and it will show up here."
      />
    );
  }

  return (
    <div className="space-y-3 pt-2">
      <div className="overflow-x-auto rounded-md border border-line">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-line bg-surface text-[10px] uppercase tracking-wider text-text-muted">
              <th className="px-3 py-2 text-left">Type</th>
              <th className="px-3 py-2 text-left">Name</th>
              <th className="px-3 py-2 text-left">Status</th>
              <th className="px-3 py-2 text-right">Metric</th>
              <th className="px-3 py-2 text-right">When</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => {
              const meta = TYPE_META[run.type];
              const Icon = meta.icon;
              return (
                <tr
                  key={`${run.type}:${run.id}`}
                  onClick={() => setSelected(run)}
                  className="cursor-pointer border-b border-line/50 transition-colors hover:bg-surface-2"
                >
                  <td className="px-3 py-2">
                    <Badge variant={meta.badge} className="gap-1 text-[10px]">
                      <Icon className="h-3 w-3" />
                      {meta.label}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">
                    <div className="font-medium text-text-primary">{run.title}</div>
                    <div className="text-[11px] capitalize text-text-muted">{run.subtitle}</div>
                  </td>
                  <td className="px-3 py-2">
                    <Badge variant={statusVariant(run.status)} className="text-[10px] capitalize">
                      {run.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-text-primary">
                    {run.metric_value != null ? `${run.metric_label} ${formatNumber(run.metric_value, { digits: 2 })}` : "—"}
                  </td>
                  <td className="px-3 py-2 text-right text-xs text-text-secondary">{formatDateTime(run.created_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <Sheet open={!!selected} onOpenChange={(open) => !open && setSelected(null)}>
        <SheetContent side="right" className="w-full sm:max-w-md">
          {selected && (
            <>
              <SheetHeader>
                <SheetTitle className="flex items-center gap-2">
                  {(() => {
                    const Icon = TYPE_META[selected.type].icon;
                    return <Icon className="h-4 w-4 text-accent" />;
                  })()}
                  {selected.title}
                </SheetTitle>
                <SheetDescription className="capitalize">
                  {TYPE_META[selected.type].label} · {selected.subtitle}
                </SheetDescription>
              </SheetHeader>

              <div className="mt-4 space-y-3 text-sm">
                <div className="flex items-center justify-between">
                  <span className="text-text-muted">Status</span>
                  <Badge variant={statusVariant(selected.status)} className="capitalize">
                    {selected.status}
                  </Badge>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-text-muted">{selected.metric_label}</span>
                  <span className="font-mono text-text-primary">
                    {formatNumber(selected.metric_value, { digits: 3 })}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-text-muted">When</span>
                  <span className="text-text-secondary">{formatDateTime(selected.created_at)}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-text-muted">Run ID</span>
                  <span className="truncate pl-3 font-mono text-[11px] text-text-secondary">{selected.id}</span>
                </div>
              </div>

              <Button
                className="mt-6 w-full gap-2"
                variant="outline"
                onClick={() => navigate(TYPE_META[selected.type].route)}
              >
                <ExternalLink className="h-4 w-4" />
                Open in {TYPE_META[selected.type].label}
              </Button>
            </>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
