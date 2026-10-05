import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { AlertTriangle, ChevronRight, Loader2 } from "lucide-react";
import { toast } from "sonner";
import {
  api,
  getJobSchedule,
  type LLMHealthResponse,
  type ProviderHealth,
  type ScheduledJob,
} from "../../lib/api";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { cn } from "../../lib/utils";
import { DataCoveragePanel } from "./components/DataCoveragePanel";
import { DkbDiagnosticsPanel } from "./components/DkbDiagnosticsPanel";
import { DkbSyncLogsTable } from "./components/DkbSyncLogsTable";
import { ResearchExtractsPanel } from "./components/ResearchExtractsPanel";
import { useAttention, useSettingsModel } from "./lib/schema";

// Provider probes older than this are history, not current state.
const OLD_RESULT_DAYS = 7;

function ago(value: string | null | undefined) {
  return value ? formatDistanceToNow(new Date(value), { addSuffix: true }) : "—";
}

function Section({ id, title, description, children }: { id: string; title: string; description?: string; children: React.ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-20 space-y-3">
      <div>
        <h3 id={`${id}-title`} className="text-base font-semibold text-text-primary">
          {title}
        </h3>
        {description ? <p className="text-sm text-text-secondary">{description}</p> : null}
      </div>
      {children}
    </section>
  );
}

export function StatusPage() {
  return (
    <div className="space-y-10">
      <header className="space-y-1">
        <h2 className="font-display text-2xl font-semibold text-text-primary">Status & jobs</h2>
        <p className="text-sm text-text-secondary">Read-only health of everything running in the background.</p>
        <nav aria-label="On this page" className="flex flex-wrap gap-x-4 gap-y-1 pt-2 text-sm">
          {[
            ["jobs", "Worker & jobs"],
            ["providers", "Data providers"],
            ["ai", "AI models"],
            ["extracts", "Research extracts"],
            ["dkb", "Bank sync"],
            ["coverage", "Price coverage"],
          ].map(([id, label]) => (
            <a key={id} href={`#${id}`} className="text-accent hover:underline">
              {label}
            </a>
          ))}
        </nav>
      </header>
      <JobsSection />
      <ProvidersSection />
      <AiSection />
      <Section id="extracts" title="Research extracts" description="Point-in-time data behind Discover's insider, analyst-revision and factor signals. The WRDS exports are refreshed by hand; a stale one leaves its signal empty.">
        <ResearchExtractsPanel />
      </Section>
      <Section id="dkb" title="Bank sync" description="FinTS self-test and the history of DKB syncs.">
        <DkbDiagnosticsPanel />
        <div className="rounded-lg border border-border bg-surface p-4">
          <h4 className="mb-3 text-sm font-medium">Sync history</h4>
          <DkbSyncLogsTable />
        </div>
      </Section>
      <Section id="coverage" title="Price coverage" description="How much bar history is stored per symbol. Pick a symbol to see its range, gaps and source.">
        <DataCoveragePanel />
      </Section>
    </div>
  );
}

function humanJob(id: string) {
  const text = id.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function ResultBadge({ status }: { status: string | null }) {
  const styles: Record<string, string> = {
    success: "bg-success/15 text-success",
    error: "bg-danger/15 text-danger",
    warning: "bg-warn/15 text-warn",
    running: "bg-info/15 text-info",
  };
  const label = status ?? "never ran";
  return <span className={cn("rounded px-1.5 py-0.5 text-xs font-medium", styles[status ?? ""] ?? "bg-surface-3 text-text-muted")}>{label}</span>;
}

function JobsTable({ jobs, retired }: { jobs: ScheduledJob[]; retired?: boolean }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-surface">
      <table className="w-full text-sm">
        <thead className="border-b border-border text-left text-xs text-text-muted">
          <tr>
            <th className="px-4 py-2 font-medium">Job</th>
            {!retired ? <th className="px-4 py-2 font-medium">Schedule</th> : null}
            <th className="px-4 py-2 font-medium">Last run</th>
            {!retired ? <th className="px-4 py-2 font-medium">Next run</th> : null}
            <th className="px-4 py-2 font-medium">Result</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {jobs.map((job) => (
            <tr key={job.id} className={cn(retired && "text-text-muted")}>
              <td className="px-4 py-2 align-top">
                <div className="font-medium">{humanJob(job.id)}</div>
                {job.last_error ? (
                  <div className="max-w-md truncate text-xs text-danger" title={job.last_error}>
                    {job.last_error}
                  </div>
                ) : null}
              </td>
              {!retired ? <td className="whitespace-nowrap px-4 py-2 align-top text-text-secondary">{job.schedule ?? "—"}</td> : null}
              <td className="whitespace-nowrap px-4 py-2 align-top text-text-secondary">{ago(job.last_run)}</td>
              {!retired ? <td className="whitespace-nowrap px-4 py-2 align-top text-text-secondary">{ago(job.next_run)}</td> : null}
              <td className="whitespace-nowrap px-4 py-2 align-top">
                <ResultBadge status={job.last_status} />
                {job.total_runs ? (
                  <span className="ml-2 text-xs text-text-muted">
                    {job.success_rate}% of {job.total_runs}
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JobsSection() {
  const schedule = useQuery({ queryKey: ["jobs", "schedule"], queryFn: getJobSchedule, refetchInterval: 60_000 });
  const attention = useAttention();
  const model = useSettingsModel();
  const pending = attention.data?.pending_restart ?? [];
  const labels = new Map(model?.entries.map((entry) => [entry.key, entry.label]) ?? []);
  const jobs = useMemo(
    () => [...(schedule.data?.jobs ?? [])].sort((a, b) => (a.last_status === "error" ? -1 : 0) - (b.last_status === "error" ? -1 : 0) || a.id.localeCompare(b.id)),
    [schedule.data],
  );
  const worker = schedule.data?.worker;

  return (
    <Section id="jobs" title="Worker & jobs" description="The background worker runs every scheduled job; DKB sync stays manual.">
      {schedule.isLoading ? <Skeleton className="h-40 w-full" /> : null}
      {schedule.isError ? <p className="text-sm text-danger">Could not load the job schedule.</p> : null}
      {schedule.data ? (
        <>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span
              className={cn("h-2 w-2 rounded-full", worker ? (worker.alive ? "bg-success" : "bg-danger") : "bg-text-muted/50")}
              aria-hidden="true"
            />
            {worker ? (
              <span>
                <span className="font-medium">{worker.alive ? "Worker running" : "Worker not responding"}</span>
                <span className="text-text-muted">
                  {" "}
                  · started {ago(worker.started_at)} · last heartbeat {ago(worker.beat_at)}
                </span>
              </span>
            ) : (
              <span className="text-text-secondary">
                The worker hasn't published its schedule yet — it does after its next restart. Until then every job name ever
                recorded is listed.
              </span>
            )}
          </div>
          {pending.length ? (
            <div role="status" className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" aria-hidden="true" />
              <span>
                Saved but not active until <code className="font-mono text-xs">quantfolio-worker</code> restarts:{" "}
                {pending.map((key) => labels.get(key) ?? key).join(", ")}.
              </span>
            </div>
          ) : null}
          <JobsTable jobs={jobs} />
          {schedule.data.retired.length ? (
            <details className="group">
              <summary className="flex cursor-pointer list-none items-center gap-1.5 text-sm text-text-secondary hover:text-text-primary">
                <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" aria-hidden="true" />
                {schedule.data.retired.length} retired job{schedule.data.retired.length === 1 ? "" : "s"} (history only, no longer scheduled)
              </summary>
              <div className="mt-3">
                <JobsTable jobs={schedule.data.retired} retired />
              </div>
            </details>
          ) : null}
        </>
      ) : null}
    </Section>
  );
}

function ProvidersSection() {
  const queryClient = useQueryClient();
  const health = useQuery<{ providers: ProviderHealth[] }>({
    queryKey: ["provider-health"],
    queryFn: () => api<{ providers: ProviderHealth[] }>("/api/market/providers/health"),
  });
  const test = useMutation({
    mutationFn: (provider: string) => api<ProviderHealth>(`/api/market/providers/${provider}/test`, { method: "POST" }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["provider-health"] });
      (data.available ? toast.success : toast.warning)(`${data.provider}: ${data.message}`);
    },
    onError: (error: Error) => toast.error(`Test failed: ${error.message}`),
  });

  const providers = useMemo(() => {
    const byProvider = new Map<string, ProviderHealth[]>();
    for (const row of health.data?.providers ?? []) {
      byProvider.set(row.provider, [...(byProvider.get(row.provider) ?? []), row]);
    }
    return [...byProvider.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [health.data]);

  return (
    <Section
      id="providers"
      title="Data providers"
      description={`Latest probe per provider and capability. Results older than ${OLD_RESULT_DAYS} days are shown faded — they describe a probe nobody re-ran.`}
    >
      {health.isLoading ? <Skeleton className="h-32 w-full" /> : null}
      {health.isError ? <p className="text-sm text-danger">Could not load provider health.</p> : null}
      {providers.length ? (
        <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
          {providers.map(([provider, rows]) => {
            const main = rows.find((row) => row.capability === "status") ?? rows[0];
            return (
              <li key={provider} className="flex flex-wrap items-start gap-3 px-4 py-3">
                <span className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", main.available ? "bg-success" : main.configured ? "bg-danger" : "bg-text-muted/50")} aria-hidden="true" />
                <div className="min-w-0 flex-1 space-y-0.5">
                  <p className="text-sm font-medium">{provider}</p>
                  {rows.map((row) => {
                    const updated = row.updated_at ?? row.last_success_at ?? null;
                    const old = updated ? Date.now() - new Date(updated).getTime() > OLD_RESULT_DAYS * 86_400_000 : false;
                    return (
                      <p key={row.capability} className={cn("text-xs", old ? "text-text-muted/60" : "text-text-secondary")}>
                        <span className="font-medium">{row.capability}</span>: {row.available ? "ok" : "unavailable"}
                        {row.message ? ` — ${row.message}` : ""}
                        {updated ? <span className="text-text-muted"> · {ago(updated)}</span> : null}
                      </p>
                    );
                  })}
                </div>
                <Button type="button" variant="outline" size="sm" disabled={test.isPending} onClick={() => test.mutate(provider)}>
                  {test.isPending && test.variables === provider ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null}
                  Test
                </Button>
              </li>
            );
          })}
        </ul>
      ) : null}
      <p className="text-xs text-text-muted">
        Keys and priority are set under{" "}
        <Link to="/settings/market-data" className="text-accent hover:underline">
          Connections › Market data
        </Link>
        .
      </p>
    </Section>
  );
}

function AiSection() {
  const health = useQuery<LLMHealthResponse>({
    queryKey: ["llm-health"],
    queryFn: () => api<LLMHealthResponse>("/api/finagent/llm/health"),
  });
  const rows: [string, boolean | undefined][] = [
    ["Local LLM", health.data?.local_llama_up],
    ["Anthropic (optional)", health.data?.anthropic_reachable],
    ["OpenAI (optional)", health.data?.openai_reachable],
  ];
  return (
    <Section id="ai" title="AI models">
      {health.isError ? <p className="text-sm text-danger">Could not load LLM health.</p> : null}
      <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
        {rows.map(([label, ok]) => (
          <li key={label} className="flex items-center gap-3 px-4 py-2.5 text-sm">
            <span className={cn("h-2 w-2 rounded-full", ok ? "bg-success" : "bg-text-muted/50")} aria-hidden="true" />
            <span className="flex-1">{label}</span>
            <span className="text-xs text-text-muted">{health.isLoading ? "checking…" : ok ? "reachable" : "not reachable / not configured"}</span>
          </li>
        ))}
      </ul>
      {health.data?.last_check ? <p className="text-xs text-text-muted">Checked {ago(health.data.last_check)}</p> : null}
    </Section>
  );
}
