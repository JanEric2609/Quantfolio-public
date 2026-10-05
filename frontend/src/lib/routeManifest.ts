import {
  Bot, BrainCircuit, CalendarCheck, Calculator, CreditCard, FlaskConical, Home, Inbox, LandPlot,
  LineChart, Newspaper, PieChart, Settings, ShieldCheck, Table2, ListChecks, FileText, Target,
  Wallet, Receipt, Repeat, Briefcase, ArrowLeftRight, Activity, Gauge, Library, Star, type LucideIcon,
} from "lucide-react";
import { isStrategyLabPath } from "../pages/lab/strategyLabTabs";

/**
 * The one list of destinations. The sidebar, the mobile sheet, the bottom nav,
 * the command palette, breadcrumbs and the document-title fallback all read it,
 * so adding a page means adding it here and nowhere else.
 */

export type NavGroupId = "today" | "money" | "ideas" | "trust" | "tools" | "admin";

export const NAV_GROUPS: { id: NavGroupId; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "money", label: "My money" },
  { id: "ideas", label: "Ideas" },
  { id: "trust", label: "Trust" },
  { id: "tools", label: "Research tools" },
  { id: "admin", label: "Admin" },
];

export interface RouteEntry {
  id: string;
  /** Where the link goes. */
  to: string;
  label: string;
  icon: LucideIcon;
  group: NavGroupId;
  /** Exact-match the path (default: the path or anything below it). */
  end?: boolean;
  /** Custom "is this entry current?" logic, for entries that own several path trees. */
  match?: (pathname: string) => boolean;
  /** Bottom-nav slot (1-based) and the label shown there. */
  mobile?: { slot: number; label: string };
  /** Collapsed behind the "Show research tools" switch. */
  researchTool?: boolean;
  /** Extra words the command palette should match. */
  keywords?: string[];
  /** false: reachable from the palette and the bottom nav, but not listed in the sidebar. */
  sidebar?: boolean;
}

export const ROUTE_ENTRIES: RouteEntry[] = [
  {
    id: "home", to: "/", label: "Home", icon: Home, group: "today", end: true,
    match: (p) => p === "/" || p === "/dashboard",
    mobile: { slot: 2, label: "Overview" },
    keywords: ["overview", "dashboard", "status"],
  },
  {
    id: "plan", to: "/plan", label: "This month", icon: CalendarCheck, group: "today", end: true,
    mobile: { slot: 1, label: "Plan" },
    keywords: ["plan", "contribution", "savings plan"],
  },
  {
    id: "decide", to: "/decide", label: "Decide", icon: Inbox, group: "today", sidebar: false,
    mobile: { slot: 4, label: "Decide" },
    keywords: ["recommendations", "accept", "reject", "snooze", "notifications", "broker"],
  },
  {
    id: "portfolio", to: "/portfolio/holdings", label: "Portfolio", icon: Briefcase, group: "money",
    match: (p) => p.startsWith("/portfolio") && !p.startsWith("/portfolio/paper"),
    mobile: { slot: 3, label: "Holdings" },
    keywords: ["holdings", "positions", "performance", "risk", "accounts", "activity"],
  },
  { id: "tax", to: "/tax", label: "Tax", icon: LandPlot, group: "money", keywords: ["tax cockpit", "kest", "vorabpauschale"] },
  {
    id: "budget", to: "/money/overview", label: "Budget", icon: CreditCard, group: "money",
    match: (p) => p.startsWith("/money"),
    keywords: ["money", "expenses", "subscriptions", "invoices", "envelopes", "income"],
  },
  { id: "discover", to: "/discover", label: "Discover", icon: BrainCircuit, group: "ideas", keywords: ["ideas", "shortlist", "dossiers"] },
  {
    id: "watchlist", to: "/research/watchlist", label: "Watchlist", icon: Star, group: "ideas",
    match: (p) => p.startsWith("/research"),
    keywords: ["watchlist", "library", "reports", "dossier", "price alert"],
  },
  {
    id: "news", to: "/news", label: "News", icon: Newspaper, group: "ideas",
    match: (p) => p === "/news" || p.startsWith("/news/"),
    keywords: ["news", "rss", "feeds", "macro", "sentiment"],
  },
  {
    id: "trust", to: "/trust", label: "Can I trust it?", icon: ShieldCheck, group: "trust",
    keywords: ["verification", "track record", "audit", "evidence"],
  },
  {
    id: "quantlab", to: "/quantlab/overview", label: "Quant Lab", icon: Calculator, group: "tools", researchTool: true,
    match: (p) => p.startsWith("/quantlab"),
  },
  {
    id: "strategy-lab", to: "/evidence", label: "Strategy lab", icon: FlaskConical, group: "tools", researchTool: true,
    match: isStrategyLabPath,
    keywords: ["evidence", "advisor", "mandates", "graduation", "paper"],
  },
  { id: "chat", to: "/chat", label: "AI Chat", icon: Bot, group: "tools", researchTool: true, keywords: ["assistant", "llm"] },
  { id: "settings", to: "/settings", label: "Control Center", icon: Settings, group: "admin", keywords: ["settings", "connections", "status"] },
];

/** Palette-only pages: real destinations that are a tab or a detail of an entry above. */
export interface ExtraPage {
  to: string;
  label: string;
  icon: LucideIcon;
  keywords?: string[];
}

export const EXTRA_PAGES: ExtraPage[] = [
  { to: "/dashboard", label: "Detailed overview", icon: PieChart, keywords: ["dashboard", "net worth"] },
  { to: "/portfolio/performance", label: "Portfolio performance", icon: LineChart },
  { to: "/portfolio/risk", label: "Portfolio risk", icon: Gauge, keywords: ["var", "drawdown"] },
  { to: "/portfolio/drift", label: "Targets & drift", icon: Target, keywords: ["rebalance", "allocation"] },
  { to: "/portfolio/accounts", label: "Accounts", icon: Wallet, keywords: ["dkb", "sync", "import"] },
  { to: "/portfolio/activity", label: "Activity", icon: Table2 },
  { to: "/portfolio/trades", label: "Trades", icon: ArrowLeftRight, keywords: ["transactions", "trade log"] },
  { to: "/portfolio/analysis", label: "Portfolio analysis", icon: Activity, keywords: ["report"] },
  { to: "/imports", label: "Imports & reconciliation", icon: FileText, keywords: ["csv"] },
  { to: "/discover?tab=shortlist", label: "Dossiers", icon: ListChecks },
  { to: "/money/expenses", label: "Expenses", icon: Receipt },
  { to: "/money/subscriptions", label: "Subscriptions", icon: Repeat },
  { to: "/money/invoices", label: "Invoices", icon: FileText },
  { to: "/research/library", label: "Research library", icon: Library },
  { to: "/settings/status", label: "System status", icon: Settings, keywords: ["worker", "jobs", "health"] },
];

export function isEntryActive(entry: RouteEntry, pathname: string): boolean {
  if (entry.match) return entry.match(pathname);
  if (entry.end) return pathname === entry.to;
  return pathname === entry.to || pathname.startsWith(`${entry.to}/`);
}

export function activeEntry(pathname: string): RouteEntry | undefined {
  return ROUTE_ENTRIES.find((entry) => isEntryActive(entry, pathname));
}

export function isResearchToolPath(pathname: string): boolean {
  return activeEntry(pathname)?.researchTool === true;
}

export interface SidebarGroup {
  id: NavGroupId;
  label: string;
  items: RouteEntry[];
}

/** Sidebar groups in display order; empty groups are dropped. */
export function sidebarGroups(): SidebarGroup[] {
  return NAV_GROUPS
    .map((group) => ({
      ...group,
      items: ROUTE_ENTRIES.filter((entry) => entry.group === group.id && entry.sidebar !== false),
    }))
    .filter((group) => group.items.length > 0);
}

/** The bottom-nav destinations in slot order. "More" is added by the BottomNav itself. */
export function mobileSlots(): (RouteEntry & { mobile: { slot: number; label: string } })[] {
  return ROUTE_ENTRIES
    .filter((entry): entry is RouteEntry & { mobile: { slot: number; label: string } } => entry.mobile !== undefined)
    .sort((a, b) => a.mobile.slot - b.mobile.slot);
}

/** Every page the command palette offers: the entries above plus the extras. */
export function paletteEntries(): { to: string; label: string; icon: LucideIcon; keywords: string[] }[] {
  const seen = new Set<string>();
  const out: { to: string; label: string; icon: LucideIcon; keywords: string[] }[] = [];
  const push = (to: string, label: string, icon: LucideIcon, keywords: string[] = []) => {
    if (seen.has(to)) return;
    seen.add(to);
    out.push({ to, label, icon, keywords });
  };
  for (const entry of ROUTE_ENTRIES) push(entry.to, entry.label, entry.icon, entry.keywords);
  for (const page of EXTRA_PAGES) push(page.to, page.label, page.icon, page.keywords);
  return out;
}

/** Title for a path when its route declares none (e.g. a route another stream adds). */
export function titleForPath(pathname: string): string | undefined {
  return activeEntry(pathname)?.label;
}

export interface Crumb {
  label: string;
  href?: string;
}

/** "My money / Portfolio" for a path inside an entry; empty when the path is in none. */
export function breadcrumbFor(pathname: string): Crumb[] {
  const entry = activeEntry(pathname);
  if (!entry) return [];
  const group = NAV_GROUPS.find((g) => g.id === entry.group);
  const crumbs: Crumb[] = [];
  if (group && entry.group !== "admin") crumbs.push({ label: group.label });
  crumbs.push({ label: entry.label });
  return crumbs;
}
