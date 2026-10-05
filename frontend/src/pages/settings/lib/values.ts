import type { SettingsCatalogEntry, SettingsUnit } from "../../../lib/api";

/** What an input shows: text for most fields, a boolean for switches. */
export type DraftValue = string | boolean;
export type Draft = Record<string, DraftValue>;

export const UNIT_SUFFIX: Record<SettingsUnit, string> = {
  fraction_pct: "%",
  pct: "%",
  pp: "pp",
  eur: "€",
  days: "days",
  hours: "hours",
  seconds: "sec",
  points: "pts",
};

// Enough precision for any threshold while hiding float noise (-0.05 * 100).
function round(value: number) {
  return Number(value.toFixed(10));
}

/** Stored value → the value its input shows. */
export function toDraft(entry: SettingsCatalogEntry, stored: unknown): DraftValue {
  if (entry.input_type === "boolean") return stored === true || stored === "true";
  if (stored === null || stored === undefined) return "";
  if (entry.input_type === "number") {
    const numeric = Number(stored);
    if (!Number.isFinite(numeric)) return String(stored);
    return String(entry.unit === "fraction_pct" ? round(numeric * 100) : numeric);
  }
  if (entry.input_type === "json") {
    return typeof stored === "string" ? stored : JSON.stringify(stored, null, 2);
  }
  return String(stored);
}

export type Parsed = { ok: true; value: unknown } | { ok: false; error: string };

/**
 * An input's value → the value to store. An empty field means "use the
 * default" and is stored as null, which deletes the override server-side.
 */
export function fromDraft(entry: SettingsCatalogEntry, draft: DraftValue | undefined): Parsed {
  if (entry.input_type === "boolean") return { ok: true, value: draft === true };
  const text = typeof draft === "string" ? draft.trim() : "";
  if (text === "") return { ok: true, value: null };

  if (entry.input_type === "number") {
    const numeric = Number(text.replace(",", "."));
    if (!Number.isFinite(numeric)) return { ok: false, error: "Enter a number" };
    const value = entry.unit === "fraction_pct" ? round(numeric / 100) : numeric;
    if (entry.min !== null && entry.min !== undefined && value < entry.min) {
      return { ok: false, error: `At least ${formatBound(entry, entry.min)}` };
    }
    if (entry.max !== null && entry.max !== undefined && value > entry.max) {
      return { ok: false, error: `At most ${formatBound(entry, entry.max)}` };
    }
    return { ok: true, value };
  }
  if (entry.input_type === "json") {
    try {
      const value = JSON.parse(text);
      if (value === null || typeof value !== "object" || Array.isArray(value)) {
        return { ok: false, error: 'Must be a JSON object, e.g. {"KEY": "value"}' };
      }
      return { ok: true, value };
    } catch {
      return { ok: false, error: "Not valid JSON" };
    }
  }
  if (entry.input_type === "date" && !/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return { ok: false, error: "Use YYYY-MM-DD" };
  }
  return { ok: true, value: text };
}

function formatBound(entry: SettingsCatalogEntry, bound: number) {
  const shown = entry.unit === "fraction_pct" ? round(bound * 100) : bound;
  return entry.unit ? `${shown} ${UNIT_SUFFIX[entry.unit]}` : String(shown);
}

/** Display text for a stored value, e.g. in a read-only summary or a "Default:" hint. */
export function formatValue(entry: SettingsCatalogEntry, stored: unknown): string {
  if (entry.input_type === "boolean") return stored ? "On" : "Off";
  if (stored === null || stored === undefined || stored === "") return "—";
  if (entry.input_type === "select") {
    return entry.option_labels?.[String(stored)] ?? String(stored);
  }
  if (entry.input_type === "json") {
    const size = typeof stored === "object" && stored ? Object.keys(stored).length : 0;
    return size ? `${size} entr${size === 1 ? "y" : "ies"}` : "None";
  }
  if (entry.input_type === "provider_chain") return "Built-in order";
  const shown = toDraft(entry, stored);
  return entry.unit ? `${shown} ${UNIT_SUFFIX[entry.unit]}` : String(shown);
}

function canonical(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value, Object.keys(value as object).sort());
  return String(value);
}

/** True when a stored value differs from the catalog default. */
export function differsFromDefault(entry: SettingsCatalogEntry, stored: unknown): boolean {
  if (entry.sensitive || entry.default === undefined) return false;
  if (entry.input_type === "boolean") return Boolean(stored) !== Boolean(entry.default);
  if (entry.input_type === "number" && stored !== null && stored !== undefined && entry.default !== null) {
    return Number(stored) !== Number(entry.default);
  }
  return canonical(stored) !== canonical(entry.default);
}

export function isDirty(entry: SettingsCatalogEntry, draft: Draft, baseline: Draft) {
  const current = draft[entry.key];
  const saved = baseline[entry.key];
  if (entry.input_type === "boolean") return Boolean(current) !== Boolean(saved);
  return String(current ?? "").trim() !== String(saved ?? "").trim();
}

export function buildDraft(entries: SettingsCatalogEntry[], settings: Record<string, unknown> | undefined): Draft {
  return Object.fromEntries(entries.map((entry) => [entry.key, toDraft(entry, settings?.[entry.key])]));
}

export type PayloadResult = { settings: Record<string, unknown>; errors: Record<string, string> };

/** Changed public settings as stored values, plus per-key validation errors. */
export function buildPayload(entries: SettingsCatalogEntry[], draft: Draft, baseline: Draft): PayloadResult {
  const settings: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const entry of entries) {
    if (entry.sensitive || !isDirty(entry, draft, baseline)) continue;
    const parsed = fromDraft(entry, draft[entry.key]);
    if (parsed.ok) settings[entry.key] = parsed.value;
    else errors[entry.key] = parsed.error;
  }
  return { settings, errors };
}

/** Anchor id for a section heading, e.g. "Monthly plan" → "monthly-plan". */
export function sectionId(section: string) {
  return section.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
}
