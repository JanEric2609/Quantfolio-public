import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  Bot,
  Building2,
  Compass,
  FlaskConical,
  Gauge,
  HardDrive,
  Home,
  Landmark,
  LineChart,
  Lock,
  ShieldCheck,
  UserCog,
  type LucideIcon,
} from "lucide-react";
import {
  getSettings,
  getSettingsAttention,
  getSettingsSchema,
  type AttentionItem,
  type SettingsArea,
  type SettingsCatalogEntry,
  type SettingsConnection,
  type SettingsPageMeta,
  type SettingsPayload,
} from "../../../lib/api";

export const AREAS: { id: SettingsArea; label: string }[] = [
  { id: "you", label: "You" },
  { id: "connections", label: "Connections" },
  { id: "engine", label: "Engine tuning" },
  { id: "system", label: "System" },
];

export const PAGE_ICONS: Record<string, LucideIcon> = {
  overview: Home,
  status: Gauge,
  profile: UserCog,
  tax: Landmark,
  bank: Building2,
  "market-data": LineChart,
  ai: Bot,
  "notes-alerts": Bell,
  discover: Compass,
  regime: Activity,
  research: FlaskConical,
  verification: ShieldCheck,
  security: Lock,
  storage: HardDrive,
};

/** The Status page is not a catalog page (no settings), but lives under System. */
export const STATUS_PAGE: SettingsPageMeta = {
  group: "status",
  path: "status",
  area: "system",
  label: "Status & jobs",
  description: "Background worker, scheduled jobs, data providers, research extracts and bank sync.",
};

/** Old Control Center slugs → their new page. */
export const LEGACY_PATHS: Record<string, string> = {
  overview: "",
  plan: "profile",
  general: "profile",
  banking: "bank",
  "ai-llm": "ai",
  "ai-research": "ai",
  "research-notes": "notes-alerts",
  notifications: "notes-alerts",
  automation: "discover",
  alphacrafter: "research",
  experimental: "regime",
  cronjobs: "status",
  integrations: "market-data",
  diagnostics: "status",
};

export const SETTINGS_KEY = ["settings"] as const;
export const SCHEMA_KEY = ["settings", "schema"] as const;
export const ATTENTION_KEY = ["settings", "attention"] as const;

export function useSettingsSchema() {
  return useQuery({ queryKey: SCHEMA_KEY, queryFn: getSettingsSchema, staleTime: 5 * 60_000 });
}

export function useSettingsValues() {
  return useQuery<SettingsPayload>({ queryKey: SETTINGS_KEY, queryFn: getSettings });
}

export function useAttention() {
  return useQuery({ queryKey: ATTENTION_KEY, queryFn: getSettingsAttention, refetchInterval: 60_000 });
}

export type SettingsModel = {
  pages: SettingsPageMeta[];
  entries: SettingsCatalogEntry[];
  connections: SettingsConnection[];
  pageByPath: Map<string, SettingsPageMeta>;
  pageByGroup: Map<string, SettingsPageMeta>;
  entriesForGroup: (group: string) => SettingsCatalogEntry[];
  connectionsForGroup: (group: string) => SettingsConnection[];
};

export function useSettingsModel(): SettingsModel | undefined {
  const schema = useSettingsSchema();
  return useMemo(() => {
    if (!schema.data) return undefined;
    const pages = [...(schema.data.pages ?? []), STATUS_PAGE];
    const entries = Object.values(schema.data.catalog ?? {});
    const connections = schema.data.connections ?? [];
    return {
      pages,
      entries,
      connections,
      pageByPath: new Map(pages.map((page) => [page.path, page])),
      pageByGroup: new Map(pages.map((page) => [page.group, page])),
      entriesForGroup: (group: string) => entries.filter((entry) => entry.group === group),
      connectionsForGroup: (group: string) => connections.filter((connection) => connection.group === group),
    };
  }, [schema.data]);
}

/** Attention items that point at a Control Center page, keyed by page path. */
export function attentionByPath(items: AttentionItem[] | undefined) {
  const byPath = new Map<string, AttentionItem[]>();
  for (const item of items ?? []) {
    const match = item.href.match(/^\/settings\/([^?#/]+)/);
    const path = match?.[1];
    if (!path) continue;
    byPath.set(path, [...(byPath.get(path) ?? []), item]);
  }
  return byPath;
}
