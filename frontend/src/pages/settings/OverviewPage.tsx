import { useMemo } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2 } from "lucide-react";
import type { SettingsPageMeta } from "../../lib/api";
import { AttentionList } from "../../components/composed/AttentionList";
import { Skeleton } from "../../components/ui/skeleton";
import { connectionStatus } from "./lib/connections";
import { AREAS, PAGE_ICONS, useAttention, useSettingsModel, useSettingsValues, type SettingsModel } from "./lib/schema";
import { differsFromDefault } from "./lib/values";

export function OverviewPage() {
  const attention = useAttention();
  const items = attention.data?.items ?? [];
  const problems = items.filter((item) => item.severity !== "info");
  const suggestions = items.filter((item) => item.severity === "info");

  return (
    <div className="space-y-8">
      <header className="space-y-1">
        <h2 className="font-display text-2xl font-semibold text-text-primary">Overview</h2>
        <p className="text-sm text-text-secondary">What needs your attention, and where everything lives.</p>
      </header>

      <section aria-labelledby="attention-title" className="space-y-2">
        <h3 id="attention-title" className="text-xs font-semibold uppercase tracking-wide text-text-muted">
          Needs attention
        </h3>
        {attention.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : attention.isError ? (
          <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 p-4 text-sm text-danger">
            Could not load the attention list: {attention.error instanceof Error ? attention.error.message : "unknown error"}
          </p>
        ) : problems.length ? (
          <AttentionList items={problems} />
        ) : (
          <div className="flex items-center gap-3 rounded-lg border border-border bg-surface p-4 text-sm">
            <CheckCircle2 className="h-5 w-5 text-success" aria-hidden="true" />
            <span>All good — nothing needs fixing.</span>
          </div>
        )}
        {suggestions.length ? (
          <details className="group">
            <summary className="cursor-pointer py-2 text-sm text-text-secondary hover:text-text-primary">
              {suggestions.length} optional suggestion{suggestions.length === 1 ? "" : "s"}
            </summary>
            <AttentionList items={suggestions} />
          </details>
        ) : null}
      </section>

      <AreaTiles />
    </div>
  );
}

function AreaTiles() {
  const model = useSettingsModel();
  const values = useSettingsValues();
  const summaries = useMemo(() => (model ? pageSummaries(model, values.data) : new Map<string, string>()), [model, values.data]);

  if (!model) return <Skeleton className="h-64 w-full" />;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {AREAS.map((area) => (
        <section key={area.id} aria-labelledby={`area-${area.id}`} className="rounded-lg border border-border bg-surface p-4">
          <h3 id={`area-${area.id}`} className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
            {area.label}
          </h3>
          <ul className="space-y-0.5">
            {model.pages
              .filter((page) => page.area === area.id)
              .map((page) => (
                <PageLink key={page.path} page={page} summary={summaries.get(page.path)} />
              ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function PageLink({ page, summary }: { page: SettingsPageMeta; summary?: string }) {
  const Icon = PAGE_ICONS[page.path];
  return (
    <li>
      <Link to={`/settings/${page.path}`} className="group flex items-center gap-3 rounded-md px-2 py-2 hover:bg-surface-2">
        {Icon ? <Icon className="h-4 w-4 shrink-0 text-text-muted group-hover:text-accent" aria-hidden="true" /> : null}
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-text-primary">{page.label}</span>
          {summary ? <span className="block truncate text-xs text-text-muted">{summary}</span> : null}
        </span>
      </Link>
    </li>
  );
}

function pageSummaries(model: SettingsModel, values: Parameters<typeof connectionStatus>[1]) {
  const catalog = Object.fromEntries(model.entries.map((entry) => [entry.key, entry]));
  const out = new Map<string, string>();
  for (const page of model.pages) {
    if (page.path === "status") {
      out.set(page.path, "Worker, jobs, providers, research extracts, bank sync");
      continue;
    }
    const connections = model.connectionsForGroup(page.group);
    if (connections.length) {
      const states = connections.map((connection) => connectionStatus(connection, values, catalog).state);
      const connected = states.filter((state) => state === "working" || state === "untested").length;
      const failing = states.filter((state) => state === "failing").length;
      out.set(
        page.path,
        connections.length === 1
          ? failing ? "Test failed" : connected ? "Connected" : "Not set up"
          : `${connected} of ${connections.length} connected${failing ? ` · ${failing} failing` : ""}`,
      );
      continue;
    }
    const entries = model.entriesForGroup(page.group).filter((entry) => !entry.sensitive);
    const changed = entries.filter((entry) => differsFromDefault(entry, values?.settings?.[entry.key] ?? entry.default)).length;
    out.set(page.path, changed ? `${changed} of ${entries.length} settings changed from default` : `${entries.length} settings, all at defaults`);
  }
  return out;
}
