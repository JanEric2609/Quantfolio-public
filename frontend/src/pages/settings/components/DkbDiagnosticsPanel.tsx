import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type DkbDiagnostic, type DkbProviderStatus } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../../components/ui/card";
import { Badge } from "../../../components/ui/badge";
import { Checkbox } from "../../../components/ui/checkbox";
import { CheckCircle2, AlertTriangle, Loader2, ChevronRight, ChevronDown } from "lucide-react";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "../../../components/ui/sheet";
import { toast } from "sonner";
import { useUiStore } from "../../../lib/store";

// Owns its own log-open state so it resets naturally when the Sheet unmounts on close.
function HistorySheetBody({ d }: { d: DkbDiagnostic }) {
  const [logOpen, setLogOpen] = useState(false);
  const lineCount = useMemo(() => d.debug_log?.split("\n").length ?? 0, [d.debug_log]);
  return (
    <div className="mt-4 space-y-3">
      <div className={`rounded-md border p-3 text-sm ${
        d.status === "passed"
          ? "border-success/40 bg-success/10 text-success"
          : d.status === "warning"
          ? "border-warn/40 bg-warn/10 text-warn"
          : "border-danger/40 bg-danger/10 text-danger"
      }`}>
        {d.summary}
      </div>
      <div className="space-y-1.5">
        <h5 className="text-xs font-semibold uppercase tracking-wide text-text-secondary">Steps</h5>
        {d.steps.filter((s) => s.key !== "debug_log").map((step) => (
          <div key={step.key} className="flex items-start gap-3 text-sm">
            {step.status === "passed" ? (
              <CheckCircle2 size={14} className="mt-0.5 text-success shrink-0" />
            ) : (
              <AlertTriangle size={14} className="mt-0.5 text-warn shrink-0" />
            )}
            <div className="min-w-0">
              <div className="font-medium text-text-primary">{step.label}</div>
              <div className="text-xs text-text-secondary break-words">{step.message}</div>
            </div>
          </div>
        ))}
      </div>
      {d.debug_log && (
        <div className="rounded-md border border-border bg-surface-2">
          <button
            aria-expanded={logOpen}
            onClick={() => setLogOpen(!logOpen)}
            className="flex w-full items-center justify-between p-3 text-sm font-medium text-text-secondary"
          >
            <span>FinTS wire log ({lineCount} lines)</span>
            {logOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
          {logOpen && (
            <pre className="max-h-80 overflow-auto border-t border-border p-3 text-xs leading-relaxed text-text-muted font-mono whitespace-pre-wrap break-all">
              {d.debug_log}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

export function DkbDiagnosticsPanel() {
  const queryClient = useQueryClient();
  const [debugLogOpen, setDebugLogOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const useTestProductId = useUiStore((s) => s.dkbUseTestPid);
  const setUseTestProductId = useUiStore((s) => s.setDkbUseTestPid);

  const diagnostics = useQuery<DkbDiagnostic[]>({
    queryKey: ["dkb-diagnostics"],
    queryFn: () => api<DkbDiagnostic[]>("/api/dkb/diagnostics"),
  });

  const providerStatus = useQuery<DkbProviderStatus>({
    queryKey: ["dkb-provider-status"],
    queryFn: () => api<DkbProviderStatus>("/api/dkb/provider/status"),
  });

  const runDiagnostics = useMutation({
    mutationFn: () => api<DkbDiagnostic>("/api/dkb/diagnostics", { method: "POST" }),
    onSuccess: (d) => {
      queryClient.invalidateQueries({ queryKey: ["dkb-diagnostics"] });
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
      if (d.status === "failed") {
        toast.error(d.summary);
      } else {
        toast.success(d.summary);
      }
    },
  });

  const runSelftest = useMutation({
    mutationFn: () => {
      const qp = useTestProductId ? "?use_test_product_id=true" : "";
      return api<DkbDiagnostic>(`/api/dkb/diagnostics/fints/selftest${qp}`, { method: "POST" });
    },
    onSuccess: (d) => {
      queryClient.invalidateQueries({ queryKey: ["dkb-diagnostics"] });
      if (d.status === "failed") {
        toast.error(d.summary);
      } else {
        toast.success(d.summary);
      }
    },
  });

  const latest = diagnostics.data?.[0];
  const running = runDiagnostics.isPending || runSelftest.isPending;
  const result = runSelftest.data ?? runDiagnostics.data ?? latest;
  const debugLogLineCount = useMemo(
    () => result?.debug_log?.split("\n").length ?? 0,
    [result?.debug_log],
  );

  return (
    <Card className="bg-surface border-border">
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-base">
          <span className="text-text-primary">DKB FinTS diagnostics</span>
          <div className="flex items-center gap-3">
            <Button
              variant="outline"
              size="sm"
              onClick={() => runDiagnostics.mutate()}
              disabled={running}
              className="h-8 text-xs"
            >
              {running && runDiagnostics.isPending ? <Loader2 size={14} className="animate-spin" /> : null}
              Run diagnostics
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => runSelftest.mutate()}
              disabled={running}
              className="h-8 text-xs"
            >
              {running && runSelftest.isPending ? <Loader2 size={14} className="animate-spin" /> : null}
              Live self-test
            </Button>
          </div>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Test PID toggle bar */}
        <div className="flex items-center justify-between rounded-md border border-border bg-surface-2 px-3 py-2 text-sm">
          <div className="flex items-center gap-2">
            <Checkbox
              id="test-pid-toggle"
              checked={useTestProductId}
              onCheckedChange={(checked) => setUseTestProductId(checked === true)}
            />
            <label htmlFor="test-pid-toggle" className="cursor-pointer select-none text-text-secondary">
              Override product ID with built-in test ID for this session
            </label>
          </div>
          {useTestProductId && (
            <Badge variant="outline" className="border-accent/40 text-accent text-xs">active</Badge>
          )}
        </div>

        {/* Connection summary */}
        {providerStatus.data && (
          <div className="rounded-md border border-border bg-surface-2 p-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="text-text-secondary">FinTS product ID</span>
              {providerStatus.data.product_id_preview ? (
                <span className="flex items-center gap-2">
                  <code className="rounded bg-surface-3 px-1.5 py-0.5 text-xs">
                    {providerStatus.data.product_id_preview}
                  </code>
                  {providerStatus.data.product_id_source && (
                    <Badge variant="secondary">{providerStatus.data.product_id_source}</Badge>
                  )}
                </span>
              ) : (
                <Badge variant="secondary">not configured (default used)</Badge>
              )}
            </div>
            <p className="mt-1 text-xs text-text-muted">
              Product ID is optional — DKB does not validate it.
            </p>
          </div>
        )}

        {/* Latest result */}
        {result && (
          <div className={`rounded-md border p-3 text-sm ${
            result.status === "passed"
              ? "border-success/40 bg-success/10 text-success"
              : result.status === "warning"
              ? "border-warn/40 bg-warn/10 text-warn"
              : "border-danger/40 bg-danger/10 text-danger"
          }`}>
            {result.summary}
          </div>
        )}

        {!result && !running && (
          <div className="rounded-md border border-border bg-surface-2 p-3 text-sm text-text-muted">
            Run diagnostics or FinTS self-test before syncing for the first time.
          </div>
        )}

        {/* Step results */}
        {result?.steps.length ? (
          <div>
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-secondary">Check results</h4>
            <div className="space-y-1.5">
              {result.steps.map((step) => (
                <div
                  key={step.key}
                  className="flex items-start gap-3 rounded-md border border-border bg-surface-2 p-3 text-sm"
                >
                  {step.status === "passed" ? (
                    <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                  ) : (
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-text-primary">{step.label}</div>
                    <div className="mt-0.5 text-xs text-text-secondary break-words">{step.message}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : result && (
          <div className="text-sm text-text-muted">No steps recorded.</div>
        )}

        {/* Debug log (collapsible, independent of history) */}
        {result?.debug_log ? (
          <div className="rounded-md border border-border bg-surface-2">
            <button
              aria-expanded={debugLogOpen}
              onClick={() => setDebugLogOpen(!debugLogOpen)}
              className="flex w-full items-center justify-between p-3 text-sm font-medium text-text-secondary"
            >
              <span>FinTS wire log ({debugLogLineCount} lines)</span>
              {debugLogOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
            {debugLogOpen && (
              <pre className="max-h-80 overflow-auto border-t border-border p-3 text-xs leading-relaxed text-text-muted font-mono whitespace-pre-wrap break-all">
                {result.debug_log}
              </pre>
            )}
          </div>
        ) : null}

        {/* History (collapsible, independent of debug log) */}
        {diagnostics.data?.length ? (
          <div className="rounded-md border border-border bg-surface-2">
            <button
              aria-expanded={historyOpen}
              onClick={() => setHistoryOpen(!historyOpen)}
              className="flex w-full items-center justify-between p-3 text-sm font-medium text-text-secondary"
            >
              <span>History ({diagnostics.data.length} diagnostic runs)</span>
              {historyOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
            {historyOpen && (
              <div className="border-t border-border p-3 space-y-1.5">
                {diagnostics.data.map((d) => (
                  <Sheet key={d.id}>
                    <SheetTrigger asChild>
                      <button
                        className="flex w-full items-center justify-between gap-2 rounded-md border border-border bg-surface p-2 text-sm transition-colors hover:bg-surface-3"
                      >
                        <div className="flex items-center gap-2">
                          <Badge
                            variant={d.status === "passed" ? "success" : d.status === "warning" ? "warning" : "danger"}
                          >
                            {d.status}
                          </Badge>
                          <span className="text-text-secondary">{new Date(d.created_at).toLocaleString()}</span>
                        </div>
                        <ChevronRight size={14} className="text-text-muted shrink-0" />
                      </button>
                    </SheetTrigger>
                    <SheetContent aria-describedby={undefined} side="right" className="bg-surface border-border">
                      <SheetHeader>
                        <SheetTitle className="text-text-primary">Diagnostic Details</SheetTitle>
                      </SheetHeader>
                      <HistorySheetBody d={d} />
                    </SheetContent>
                  </Sheet>
                ))}
              </div>
            )}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
