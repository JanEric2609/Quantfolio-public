import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Upload, XCircle } from "lucide-react";
import { DkbSyncButton } from "../../components/portfolio/DkbSyncButton";
import { ScalableReview, ScalableSavingsPlans, ScalableSyncButton, useScalableStatus } from "../../components/portfolio/ScalableSync";
import { Button } from "../../components/ui/button";
import { Card, CardContent } from "../../components/ui/card";
import { AccountsTable } from "./components/AccountsTable";
import { api, type DkbDiagnostic, type WealthSummary } from "../../lib/api";
import { formatDateTime } from "../../lib/format";

type Account = WealthSummary["accounts"][number];

function DkbDiagnosticsSummary() {
  const { data } = useQuery<DkbDiagnostic[]>({
    queryKey: ["dkb-diagnostics"],
    queryFn: () => api<DkbDiagnostic[]>("/api/dkb/diagnostics"),
  });
  const latest = data?.[0];
  if (!latest) return null;

  const config = {
    passed: {
      color: "border-success/40 bg-success/10 text-success hover:bg-success/20",
      icon: CheckCircle2,
      label: "DKB: all checks passed",
    },
    warning: {
      color: "border-warn/40 bg-warn/10 text-warn hover:bg-warn/20",
      icon: AlertTriangle,
      label: "DKB: warnings",
    },
    failed: {
      color: "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20",
      icon: XCircle,
      label: "DKB: issues found",
    },
  }[latest.status];
  const Icon = config.icon;

  return (
    <Link
      to="/settings/status#dkb"
      className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${config.color}`}
      title={`${latest.summary} — open Control Center → Status → Bank sync for full diagnostics`}
    >
      <Icon size={12} aria-hidden="true" />
      <span>{config.label}</span>
    </Link>
  );
}

/** One compact row per broker: name, status, Sync. */
function ConnectionRow({ name, status, children }: { name: string; status?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 py-2 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="w-20 shrink-0 text-sm font-medium text-text-primary">{name}</span>
        {status}
      </div>
      <div className="flex min-w-0 items-center sm:justify-end">{children}</div>
    </div>
  );
}

function ConnectionsCard() {
  const accounts = useQuery({ queryKey: ["portfolio-accounts"], queryFn: () => api<Account[]>("/api/portfolio/accounts") });
  const scalable = useScalableStatus();
  const dkbSynced = (accounts.data ?? [])
    .filter((a) => a.source === "dkb" && a.last_synced)
    .map((a) => a.last_synced as string)
    .sort()
    .at(-1);
  return (
    <Card>
      <CardContent className="divide-y divide-border pb-3 pt-4">
        <h2 className="pb-2 text-xs font-semibold uppercase tracking-wide text-text-secondary">Connections</h2>
        <ConnectionRow
          name="DKB"
          status={
            <>
              <DkbDiagnosticsSummary />
              {dkbSynced && <span className="text-xs text-text-secondary">synced {formatDateTime(dkbSynced)}</span>}
            </>
          }
        >
          {/* The Test PID switch lives in Control Center, under the DKB diagnostics. */}
          <DkbSyncButton showTestPid={false} />
        </ConnectionRow>
        {scalable.data?.enabled && (
          <ConnectionRow name="Scalable">
            <ScalableSyncButton />
          </ConnectionRow>
        )}
      </CardContent>
    </Card>
  );
}

export function AccountsTab() {
  return (
    <div className="space-y-4">
      <ScalableReview />
      <ConnectionsCard />
      <AccountsTable
        rightSlot={
          <Button asChild variant="ghost" size="sm" className="text-text-secondary">
            <Link to="/imports"><Upload className="mr-1 h-3.5 w-3.5" aria-hidden="true" /> Import CSV</Link>
          </Button>
        }
      />
      <ScalableSavingsPlans />
    </div>
  );
}
