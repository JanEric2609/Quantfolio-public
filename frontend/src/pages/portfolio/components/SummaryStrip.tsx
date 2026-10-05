import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { BarChart3, AlertTriangle } from "lucide-react";
import { api, type WealthSummary } from "../../../lib/api";
import { formatCurrency, formatDate, formatSignedDelta } from "../../../lib/format";
import { DeltaText } from "../../../components/composed/DeltaText";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Skeleton } from "../../../components/ui/skeleton";

export function SummaryStrip() {
  const navigate = useNavigate();
  const wealth = useQuery({ queryKey: ["wealth"], queryFn: () => api<WealthSummary>("/api/portfolio/wealth") });

  if (wealth.isLoading) {
    return (
      <div className="lg:sticky lg:top-14 z-20 grid grid-cols-1 gap-3 rounded-md border border-border bg-surface/95 p-3 backdrop-blur lg:grid-cols-[0.8fr_0.8fr_1.4fr_0.8fr_auto]">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }

  if (wealth.isError) {
    return (
      <div className="lg:sticky lg:top-14 z-20 rounded-md border border-danger/30 bg-surface/95 p-3 backdrop-blur">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm text-danger">
            <AlertTriangle className="h-4 w-4" />
            Failed to load portfolio summary.
          </div>
          <Button variant="outline" size="sm" onClick={() => wealth.refetch()}>Retry</Button>
        </div>
      </div>
    );
  }

  const breakdown = Object.entries((wealth.data?.positions ?? []).reduce<Record<string, number>>((totals, position) => {
    const key = position.asset_type ?? position.source;
    totals[key] = (totals[key] ?? 0) + position.current_value;
    return totals;
  }, {})).slice(0, 4);
  const pnl = wealth.data?.cashflow_30d?.net ?? 0;
  // Server wealth figures are computed in EUR (no FX conversion exists yet), so
  // label them with the server's declared currency, never the reporting-currency
  // setting — which would relabel EUR numbers as $/£ without converting them.
  const reportCurrency = wealth.data?.currency ?? "EUR";
  return (
    <div className="lg:sticky lg:top-14 z-20 grid grid-cols-1 gap-3 rounded-md border border-border bg-surface/95 p-3 backdrop-blur lg:grid-cols-[0.8fr_0.8fr_1.4fr_0.8fr_auto]">
      <Stat label="Net worth" value={formatCurrency(wealth.data?.total_value, { currency: reportCurrency })} />
      <Stat label="30d cashflow" value={<DeltaText delta={formatSignedDelta(pnl, "currency", { currency: reportCurrency })} />} />
      <div>
        <div className="text-xs text-text-secondary">Asset classes</div>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {breakdown.map(([name, value]) => <Badge key={name} variant="secondary">{name} {formatCurrency(value, { currency: reportCurrency })}</Badge>)}
          {!breakdown.length && <span className="text-sm text-text-muted">No positions</span>}
        </div>
      </div>
      <Stat label="DKB last sync" value={wealth.data?.last_snapshot ? formatDate(wealth.data.last_snapshot) : "Not recorded"} />
      <div className="flex items-center">
        <Button variant="ghost" size="sm" onClick={() => navigate("/quantlab/overview")} className="gap-1 text-xs" title="Open in Quant Lab">
          <BarChart3 className="h-3.5 w-3.5" />
          Quant Lab
        </Button>
      </div>
    </div>
  );
}

function Stat({ label, value, valueClass = "" }: { label: string; value: React.ReactNode; valueClass?: string }) {
  return <div><div className="text-xs text-text-secondary">{label}</div><div className={`mt-1 font-mono text-sm tabular-nums ${valueClass}`}>{value}</div></div>;
}
