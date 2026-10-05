import { useEffect, useMemo, useState } from "react";
import { Link, Navigate, useLocation, useParams, useSearchParams } from "react-router-dom";
import { AlertTriangle, ChevronRight, Info, LockKeyhole } from "lucide-react";
import type { SettingsCatalogEntry, SettingsPageMeta } from "../../lib/api";
import { Skeleton } from "../../components/ui/skeleton";
import { cn } from "../../lib/utils";
import { ConnectionList } from "./components/ConnectionList";
import { PasskeyList } from "./components/PasskeyList";
import { SaveBar } from "./components/SaveBar";
import { SettingRow } from "./components/SettingRow";
import { DkbSetupWizardButton } from "./components/SetupWizard";
import { LEGACY_PATHS, useAttention, useSettingsModel, useSettingsValues } from "./lib/schema";
import { useDraft, useLeaveGuard, useSaveSettings } from "./lib/useSettingsForm";
import { sectionId } from "./lib/values";

type Section = { title: string; entries: SettingsCatalogEntry[] };

function groupSections(entries: SettingsCatalogEntry[]): Section[] {
  const sections: Section[] = [];
  for (const entry of entries) {
    const title = entry.section ?? "General";
    const existing = sections.find((section) => section.title === title);
    if (existing) existing.entries.push(entry);
    else sections.push({ title, entries: [entry] });
  }
  return sections;
}

export function SettingsPage() {
  const { page: path = "" } = useParams();
  const model = useSettingsModel();

  if (path in LEGACY_PATHS) return <Navigate to={`/settings/${LEGACY_PATHS[path]}`} replace />;
  if (!model) return <PageSkeleton />;
  const page = model.pageByPath.get(path);
  if (!page || page.path === "status") return <Navigate to="/settings" replace />;
  return <CatalogPage key={page.group} page={page} />;
}

function CatalogPage({ page }: { page: SettingsPageMeta }) {
  const model = useSettingsModel()!;
  const values = useSettingsValues();
  const attention = useAttention();
  const [params] = useSearchParams();
  const location = useLocation();
  const catalog = useMemo(() => Object.fromEntries(model.entries.map((entry) => [entry.key, entry])), [model.entries]);

  const entries = useMemo(
    () => model.entriesForGroup(page.group).filter((entry) => !entry.sensitive && !entry.connection),
    [model, page.group],
  );
  const connections = model.connectionsForGroup(page.group);
  const basic = useMemo(() => groupSections(entries.filter((entry) => !entry.advanced)), [entries]);
  const advanced = useMemo(() => groupSections(entries.filter((entry) => entry.advanced)), [entries]);
  const advancedCount = entries.filter((entry) => entry.advanced).length;

  const form = useDraft(entries, values.data);
  const save = useSaveSettings(page.label);
  const dirty = form.dirtyKeys.size > 0;
  useLeaveGuard(dirty);

  // Deep links: ?focus=<key> (search results) and #<section> (other pages).
  const focus = params.get("focus");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  useEffect(() => {
    if (!focus || !values.data) return;
    if (catalog[focus]?.advanced) setAdvancedOpen(true);
    setHighlighted(focus);
    const timer = window.setTimeout(() => {
      document.getElementById(`row-${focus}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
      document.getElementById(`setting-${focus}`)?.focus({ preventScroll: true });
    }, 50);
    const clear = window.setTimeout(() => setHighlighted(null), 2500);
    return () => {
      window.clearTimeout(timer);
      window.clearTimeout(clear);
    };
  }, [focus, catalog, values.data]);
  useEffect(() => {
    if (!location.hash || !values.data) return;
    document.getElementById(location.hash.slice(1))?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [location.hash, values.data]);

  const pendingHere = (attention.data?.pending_restart ?? []).filter((key) => catalog[key]?.group === page.group);

  const renderSections = (sections: Section[]) =>
    sections.map((section) => (
      <section key={section.title} id={sectionId(section.title)} aria-labelledby={`${sectionId(section.title)}-title`} className="scroll-mt-20 space-y-2">
        <h3 id={`${sectionId(section.title)}-title`} className="text-xs font-semibold uppercase tracking-wide text-text-muted">
          {section.title}
        </h3>
        <div className="divide-y divide-border rounded-lg border border-border bg-surface px-4">
          {section.entries.map((entry) => (
            <SettingRow
              key={entry.key}
              entry={entry}
              value={form.draft[entry.key]}
              dirty={form.dirtyKeys.has(entry.key)}
              error={form.errors[entry.key]}
              highlighted={highlighted === entry.key}
              onChange={(value) => form.setValue(entry.key, value)}
            />
          ))}
        </div>
      </section>
    ));

  if (values.isLoading) return <PageSkeleton />;

  return (
    <div className="space-y-8">
      <header className="space-y-1">
        <h2 className="font-display text-2xl font-semibold text-text-primary">{page.label}</h2>
        <p className="max-w-prose text-sm text-text-secondary">{page.description}</p>
      </header>

      {pendingHere.length ? (
        <div role="status" className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" aria-hidden="true" />
          <span>
            Saved, waiting for a worker restart: {pendingHere.map((key) => catalog[key].label).join(", ")}.{" "}
            <Link to="/settings/status" className="text-accent hover:underline">
              Status
            </Link>
          </span>
        </div>
      ) : null}

      <PageIntro page={page} dkbConnected={!!values.data?.integrations?.dkb?.configured} />

      {connections.length ? (
        <section aria-labelledby="connections-title" className="space-y-2">
          <ConnectionList headingId="connections-title" connections={connections} catalog={catalog} values={values.data} />
        </section>
      ) : null}

      <form
        className="space-y-8"
        onSubmit={(event) => {
          event.preventDefault();
          const settings = form.collect();
          if (settings && Object.keys(settings).length) save.mutate({ settings });
        }}
      >
        {renderSections(basic)}

        {advanced.length ? (
          <details
            open={advancedOpen}
            onToggle={(event) => setAdvancedOpen((event.target as HTMLDetailsElement).open)}
            className="group space-y-6"
          >
            <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium text-text-secondary hover:text-text-primary">
              <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" aria-hidden="true" />
              Advanced settings
              <span className="text-text-muted">({advancedCount})</span>
            </summary>
            <div className="mt-4 space-y-8">{renderSections(advanced)}</div>
          </details>
        ) : null}

        {!entries.length && !connections.length ? (
          <p className="text-sm text-text-muted">Nothing to configure on this page.</p>
        ) : null}

        <SaveBar
          changes={form.dirtyKeys.size}
          errors={Object.keys(form.errors).length}
          saving={save.isPending}
          onSave={() => {
            const settings = form.collect();
            if (settings && Object.keys(settings).length) save.mutate({ settings });
          }}
          onDiscard={form.discard}
        />
      </form>
    </div>
  );
}

/** Page-specific context that isn't a setting. */
function PageIntro({ page, dkbConnected }: { page: SettingsPageMeta; dkbConnected: boolean }) {
  if (page.group === "tax") {
    return (
      <Note icon={Info}>
        Estimates only — not tax advice. Your broker statements are the source of truth. The{" "}
        <Link to="/tax" className="text-accent hover:underline">
          Tax Cockpit
        </Link>{" "}
        uses these inputs.
      </Note>
    );
  }
  if (page.group === "banking" && dkbConnected) {
    return (
      <Note icon={Info}>
        Sync history and FinTS diagnostics are on{" "}
        <Link to="/settings/status#dkb" className="text-accent hover:underline">
          Status › Bank sync
        </Link>
        . Start a sync from{" "}
        <Link to="/portfolio/accounts" className="text-accent hover:underline">
          Portfolio › Accounts
        </Link>
        .
      </Note>
    );
  }
  if (page.group === "banking") {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-surface p-4">
        <div className="text-sm">
          <p className="font-medium">First time connecting DKB?</p>
          <p className="text-text-secondary">
            The guided setup stores your credentials and runs the FinTS self-test. Sync history and diagnostics are on{" "}
            <Link to="/settings/status#dkb" className="text-accent hover:underline">
              Status
            </Link>
            .
          </p>
        </div>
        <DkbSetupWizardButton />
      </div>
    );
  }
  if (page.group === "security") {
    return (
      <div className="space-y-4">
        <PasskeyList />
        <Note icon={LockKeyhole}>
          API keys and bank credentials are encrypted at rest with the server's ENCRYPTION_KEY and are never sent back to the
          browser.
        </Note>
      </div>
    );
  }
  return null;
}

function Note({ icon: Icon, children }: { icon: typeof Info; children: React.ReactNode }) {
  return (
    <p className={cn("flex items-start gap-2 rounded-md border border-border bg-surface-2 p-3 text-sm text-text-secondary")}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-text-muted" aria-hidden="true" />
      <span>{children}</span>
    </p>
  );
}

export function PageSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-4 w-96" />
      <Skeleton className="h-48 w-full" />
      <Skeleton className="h-32 w-full" />
    </div>
  );
}
