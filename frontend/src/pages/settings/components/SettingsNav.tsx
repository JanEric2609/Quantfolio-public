import { useMemo, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { Search, X } from "lucide-react";
import type { AttentionItem, SettingsPageMeta } from "../../../lib/api";
import { Input } from "../../../components/ui/input";
import { cn } from "../../../lib/utils";
import { AREAS, PAGE_ICONS, attentionByPath, useAttention, useSettingsModel, type SettingsModel } from "../lib/schema";

type SearchResult = { id: string; title: string; context: string; to: string };

function search(model: SettingsModel, query: string): SearchResult[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const matches = (...fields: (string | null | undefined)[]) => fields.some((field) => field?.toLowerCase().includes(q));
  const results: SearchResult[] = [];

  for (const page of model.pages) {
    if (matches(page.label, page.description)) {
      results.push({ id: `page-${page.path}`, title: page.label, context: "Page", to: `/settings/${page.path}` });
    }
  }
  for (const connection of model.connections) {
    const page = model.pageByGroup.get(connection.group);
    if (page && matches(connection.label, connection.description, connection.id)) {
      results.push({
        id: `connection-${connection.id}`,
        title: connection.label,
        context: `${page.label} › Connection`,
        to: `/settings/${page.path}?connection=${connection.id}`,
      });
    }
  }
  for (const entry of model.entries) {
    const page = model.pageByGroup.get(entry.group);
    if (!page) continue;
    const optionLabels = Object.values(entry.option_labels ?? {}).join(" ");
    if (!matches(entry.label, entry.help, entry.key, entry.section, optionLabels)) continue;
    const connection = entry.connection ? model.connections.find((c) => c.id === entry.connection) : undefined;
    results.push({
      id: `entry-${entry.key}`,
      title: entry.label,
      context: [page.label, connection?.label ?? entry.section].filter(Boolean).join(" › "),
      to: connection ? `/settings/${page.path}?connection=${connection.id}` : `/settings/${page.path}?focus=${entry.key}`,
    });
  }
  return results.slice(0, 25);
}

function Count({ items }: { items: AttentionItem[] | undefined }) {
  const relevant = (items ?? []).filter((item) => item.severity !== "info");
  if (!relevant.length) return null;
  const error = relevant.some((item) => item.severity === "error");
  return (
    <span
      className={cn(
        "ml-auto min-w-5 rounded-full px-1.5 text-center text-[10px] font-semibold leading-5",
        error ? "bg-danger/15 text-danger" : "bg-warn/15 text-warn",
      )}
      aria-label={`${relevant.length} item${relevant.length === 1 ? "" : "s"} need attention`}
    >
      {relevant.length}
    </span>
  );
}

function NavItem({ page, items, end }: { page: Pick<SettingsPageMeta, "path" | "label">; items?: AttentionItem[]; end?: boolean }) {
  const Icon = PAGE_ICONS[page.path || "overview"];
  return (
    <NavLink
      to={page.path ? `/settings/${page.path}` : "/settings"}
      end={end}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors",
          isActive ? "bg-accent/15 font-medium text-text-primary" : "text-text-secondary hover:bg-surface-2 hover:text-text-primary",
        )
      }
    >
      {Icon ? <Icon className="h-4 w-4 shrink-0" aria-hidden="true" /> : null}
      <span className="truncate">{page.label}</span>
      <Count items={items} />
    </NavLink>
  );
}

export function SettingsNav() {
  const model = useSettingsModel();
  const attention = useAttention();
  const [query, setQuery] = useState("");
  const results = useMemo(() => (model ? search(model, query) : []), [model, query]);
  const byPath = useMemo(() => attentionByPath(attention.data?.items), [attention.data]);

  return (
    <nav aria-label="Control Center" className="space-y-4">
      <div className="relative">
        <label htmlFor="settings-search" className="sr-only">
          Search settings
        </label>
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" aria-hidden="true" />
        <Input
          id="settings-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => event.key === "Escape" && setQuery("")}
          placeholder="Search settings"
          className="pl-8 pr-8"
          autoComplete="off"
        />
        {query ? (
          <button type="button" onClick={() => setQuery("")} aria-label="Clear search"
            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-primary">
            <X className="h-4 w-4" />
          </button>
        ) : null}
      </div>

      {query ? (
        <ul className="space-y-0.5" aria-label="Search results">
          {results.map((result) => (
            <li key={result.id}>
              <Link to={result.to} onClick={() => setQuery("")} className="block rounded-md px-2.5 py-1.5 hover:bg-surface-2">
                <span className="block text-sm text-text-primary">{result.title}</span>
                <span className="block text-xs text-text-muted">{result.context}</span>
              </Link>
            </li>
          ))}
          {!results.length ? <li className="px-2.5 py-4 text-sm text-text-muted">No settings match “{query}”.</li> : null}
        </ul>
      ) : (
        <div className="space-y-4">
          <NavItem page={{ path: "", label: "Overview" }} end items={attention.data?.items.filter((item) => !item.href.startsWith("/settings/"))} />
          {AREAS.map((area) => {
            const pages = (model?.pages ?? []).filter((page) => page.area === area.id);
            if (!pages.length) return null;
            return (
              <div key={area.id} className="space-y-0.5">
                <p className="px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wider text-text-muted">{area.label}</p>
                {pages.map((page) => (
                  <NavItem key={page.path} page={page} items={byPath.get(page.path)} />
                ))}
              </div>
            );
          })}
        </div>
      )}
    </nav>
  );
}

/** Compact page picker for narrow screens, where the sidebar is hidden. */
export function SettingsPagePicker() {
  const model = useSettingsModel();
  const location = useLocation();
  const navigate = useNavigate();
  const current = location.pathname.replace(/^\/settings\/?/, "").split("/")[0] ?? "";

  return (
    <div>
      <label htmlFor="settings-page-picker" className="sr-only">
        Control Center page
      </label>
      <select
        id="settings-page-picker"
        value={current}
        onChange={(event) => navigate(event.target.value ? `/settings/${event.target.value}` : "/settings")}
        className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text-primary"
      >
        <option value="">Overview</option>
        {AREAS.map((area) => (
          <optgroup key={area.id} label={area.label}>
            {(model?.pages ?? [])
              .filter((page) => page.area === area.id)
              .map((page) => (
                <option key={page.path} value={page.path}>
                  {page.label}
                </option>
              ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}
