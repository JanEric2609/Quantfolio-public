import { formatDateTime } from "../../lib/format";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, CircleSlash, XCircle } from "lucide-react";
import { Card } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { api } from "../../lib/api";
import type {
  AdvisorDiagnosticCheck,
  AdvisorDiagnosticStatus,
  AdvisorDiagnosticsReport,
} from "../../lib/api";

const STATUS_ICON: Record<AdvisorDiagnosticStatus, typeof CheckCircle2> = {
  ok: CheckCircle2,
  warn: AlertTriangle,
  fail: XCircle,
  skipped: CircleSlash,
};

const STATUS_CLASS: Record<AdvisorDiagnosticStatus, string> = {
  ok: "text-success",
  warn: "text-warn",
  fail: "text-danger",
  skipped: "text-text-muted",
};

const STATUS_LABEL: Record<AdvisorDiagnosticStatus, string> = {
  ok: "OK",
  warn: "Warning",
  fail: "Blocked",
  skipped: "Not checked",
};

function StatusIcon({ status }: { status: AdvisorDiagnosticStatus }) {
  const Icon = STATUS_ICON[status];
  return <Icon className={`h-4 w-4 shrink-0 ${STATUS_CLASS[status]}`} aria-hidden />;
}

function CheckRow({ check, index }: { check: AdvisorDiagnosticCheck; index: number }) {
  const [open, setOpen] = useState(check.status === "fail");
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <li className="border-b border-border/60 last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 py-3 text-left hover:bg-surface-hover/40"
      >
        <span className="pt-0.5 text-xs tabular-nums text-text-muted w-5">{index + 1}</span>
        <StatusIcon status={check.status} />
        <span className="flex-1 min-w-0">
          <span className="flex items-center gap-2">
            <span className="text-sm font-medium text-text-primary">{check.label}</span>
            <span className={`text-[10px] uppercase tracking-wide ${STATUS_CLASS[check.status]}`}>
              {STATUS_LABEL[check.status]}
            </span>
          </span>
          <span className="block text-xs text-text-secondary mt-0.5">{check.detail}</span>
          {check.remedy && check.status !== "ok" && (
            <span className="block text-xs text-text-muted mt-1">Fix: {check.remedy}</span>
          )}
        </span>
        <Chevron className="h-4 w-4 shrink-0 text-text-muted mt-0.5" aria-hidden />
      </button>
      {open && (
        <pre className="mb-3 ml-8 max-h-80 overflow-auto rounded bg-surface-raised p-3 text-[11px] leading-relaxed text-text-secondary">
          {JSON.stringify(check.data, null, 2)}
        </pre>
      )}
    </li>
  );
}

export function DiagnosticsTab() {
  const [probeLlm, setProbeLlm] = useState(false);

  const query = useQuery({
    queryKey: ["advisor-diagnostics", probeLlm],
    queryFn: () =>
      api<AdvisorDiagnosticsReport>(`/api/advisor/diagnostics?probe_llm=${probeLlm}`),
  });

  if (query.isLoading) {
    return <Card className="p-6 text-sm text-text-muted">Running diagnostics…</Card>;
  }
  if (query.isError || !query.data) {
    return (
      <Card className="p-6 text-sm text-danger">
        Diagnostics failed to load: {(query.error as Error)?.message ?? "unknown error"}
      </Card>
    );
  }

  const report = query.data;

  return (
    <div className="space-y-4">
      <Card className="p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <StatusIcon status={report.status} />
            <div>
              <h3 className="text-sm font-semibold text-text-primary">Loop health</h3>
              <p className="text-sm text-text-secondary mt-0.5">{report.summary}</p>
              <p className="text-xs text-text-muted mt-1">
                {report.counts.ok} ok · {report.counts.warn} warning · {report.counts.fail} blocked ·{" "}
                {report.counts.skipped} not checked · generated{" "}
                {formatDateTime(report.generated_at)}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant={probeLlm ? "default" : "outline"}
              size="sm"
              onClick={() => setProbeLlm((v) => !v)}
            >
              {probeLlm ? "LLM probe on" : "Probe LLM"}
            </Button>
            <Button variant="outline" size="sm" onClick={() => query.refetch()} disabled={query.isFetching}>
              {query.isFetching ? "Checking…" : "Re-run"}
            </Button>
          </div>
        </div>

        {report.blocking.length > 0 && (
          <div className="mt-4 rounded border border-danger/40 bg-danger/5 p-3">
            <h4 className="text-xs font-semibold text-danger mb-1">
              Fix these in order — each one starves everything below it
            </h4>
            <ol className="list-decimal pl-4 text-xs text-text-secondary space-y-1">
              {report.blocking.map((b) => (
                <li key={b.key}>
                  <span className="font-medium text-text-primary">{b.label}</span> — {b.detail}
                  {b.remedy && <span className="block text-text-muted">Fix: {b.remedy}</span>}
                </li>
              ))}
            </ol>
          </div>
        )}

        {/* Stages that cannot compute until a prediction horizon elapses. Listed
            apart from the blockers so "waiting" never reads as "broken". */}
        {report.pending?.length > 0 && (
          <div className="mt-3 rounded border border-border bg-surface-raised/60 p-3">
            <h4 className="text-xs font-semibold text-text-secondary mb-1">
              Waiting on data that has not matured yet — nothing to fix here
            </h4>
            <ul className="list-disc pl-4 text-xs text-text-muted space-y-1">
              {report.pending.map((p) => (
                <li key={p.key}>
                  <span className="font-medium text-text-secondary">{p.label}</span> — {p.detail}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Card>

      <Card className="p-6">
        <h3 className="text-sm font-semibold text-text-primary mb-1">Pipeline stages</h3>
        <p className="text-xs text-text-muted mb-2">
          Listed in execution order: scheduler → candidates → prices → LLM → cycle → trades →
          predictions → scorecard → learning. Expand a stage for the raw numbers.
        </p>
        <ul>
          {report.checks.map((check, i) => (
            <CheckRow key={check.key} check={check} index={i} />
          ))}
        </ul>
      </Card>
    </div>
  );
}
