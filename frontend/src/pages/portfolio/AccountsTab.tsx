import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Upload, XCircle } from "lucide-react";
import { DkbSyncButton } from "../../components/portfolio/DkbSyncButton";
import { ScalableReview, ScalableSavingsPlans, ScalableSyncButton } from "../../components/portfolio/ScalableSync";
import { AccountsTable } from "./components/AccountsTable";
import { api, type DkbDiagnostic } from "../../lib/api";

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

export function AccountsTab() {
  return (
    <div className="space-y-4">
      <ScalableReview />
      <AccountsTable
        rightSlot={
          <div className="flex flex-col items-end gap-2">
            <DkbDiagnosticsSummary />
            <DkbSyncButton />
            <ScalableSyncButton />
            <Link to="/imports" className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline">
              <Upload className="h-3.5 w-3.5" aria-hidden="true" /> Import CSV
            </Link>
          </div>
        }
      />
      <ScalableSavingsPlans />
    </div>
  );
}
