import { ExternalLink, RotateCcw } from "lucide-react";
import type { SettingsCatalogEntry } from "../../../lib/api";
import { Badge } from "../../../components/ui/badge";
import { cn } from "../../../lib/utils";
import { differsFromDefault, formatValue, fromDraft, type DraftValue } from "../lib/values";
import { HelpPopover } from "./HelpPopover";
import { SettingControl } from "./SettingControl";

const WIDE_INPUTS = new Set(["json", "provider_chain"]);

/** Value that puts a field back to its default: empty for free inputs, the default itself for choices. */
export function resetValue(entry: SettingsCatalogEntry): DraftValue {
  if (entry.input_type === "boolean") return Boolean(entry.default);
  if (entry.input_type === "select") return String(entry.default ?? "");
  return "";
}

export function SettingRow({
  entry,
  value,
  dirty,
  error,
  highlighted,
  stacked,
  onChange,
}: {
  entry: SettingsCatalogEntry;
  value: DraftValue | undefined;
  dirty: boolean;
  error?: string;
  highlighted?: boolean;
  /** Label above the control (narrow containers such as the connection drawer). */
  stacked?: boolean;
  onChange: (value: DraftValue) => void;
}) {
  const helpId = `setting-${entry.key}-help`;
  const errorId = `setting-${entry.key}-error`;
  const parsed = fromDraft(entry, value);
  const effective = parsed.ok ? (parsed.value ?? entry.default) : undefined;
  const changedFromDefault = parsed.ok && entry.default !== undefined && differsFromDefault(entry, effective);
  const wide = (stacked && entry.input_type !== "boolean") || WIDE_INPUTS.has(entry.input_type);
  const safeDocUrl = entry.doc_url && /^https?:\/\//i.test(entry.doc_url) ? entry.doc_url : null;

  return (
    <div
      id={`row-${entry.key}`}
      className={cn(
        "grid gap-x-8 gap-y-3 py-4 transition-colors",
        wide ? "grid-cols-1" : "sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center",
        highlighted && "-mx-3 rounded-md bg-accent/10 px-3",
      )}
    >
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={`setting-${entry.key}`} className="text-sm font-medium text-text-primary">
            {entry.label}
          </label>
          {dirty ? <span className="h-1.5 w-1.5 rounded-full bg-warn" title="Unsaved change" aria-label="Unsaved change" /> : null}
          {entry.status === "needs_worker_restart" ? (
            <Badge variant="outline" className="border-warn/40 px-1.5 py-0 text-[10px] font-normal text-warn" title="Takes effect after the background worker restarts">
              Applies after worker restart
            </Badge>
          ) : null}
          {entry.status === "experimental" ? (
            <Badge variant="outline" className="border-info/40 px-1.5 py-0 text-[10px] font-normal text-info">
              Experimental
            </Badge>
          ) : null}
          {entry.extended_help ? (
            <HelpPopover title={entry.label} docUrl={entry.doc_url}>
              {entry.extended_help}
            </HelpPopover>
          ) : null}
        </div>
        <p id={helpId} className="max-w-prose text-sm text-text-secondary">
          {entry.help}
        </p>
        {error ? (
          <p id={errorId} role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
          {changedFromDefault ? (
            <>
              <span>Default: {formatValue(entry, entry.default)}</span>
              <button
                type="button"
                onClick={() => onChange(resetValue(entry))}
                className="inline-flex items-center gap-1 text-accent hover:underline"
              >
                <RotateCcw className="h-3 w-3" aria-hidden="true" /> Reset
              </button>
            </>
          ) : null}
          {safeDocUrl && !entry.extended_help ? (
            <a href={safeDocUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
              Docs <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          ) : null}
        </div>
      </div>
      <div className={cn(!wide && "sm:justify-self-end")}>
        <SettingControl
          entry={entry}
          value={value}
          onChange={onChange}
          invalid={!!error}
          describedBy={error ? `${helpId} ${errorId}` : helpId}
        />
      </div>
    </div>
  );
}
