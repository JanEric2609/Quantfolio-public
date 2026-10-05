import { create } from "zustand";

type ViewMode = "desktop" | "mobile";
type Density  = "comfortable" | "compact";
/** Which synced broker the position views show; "all" includes hand-entered holdings. */
export type BrokerFilter = "all" | "dkb" | "scalable";
const BROKER_FILTERS: readonly BrokerFilter[] = ["all", "dkb", "scalable"];

interface UiState {
  viewMode: ViewMode;
  density: Density;
  searchOpen: boolean;
  setViewMode(v: ViewMode): void;
  setDensity(d: Density): void;
  setSearchOpen(open: boolean): void;
  dkbUseTestPid: boolean;
  setDkbUseTestPid(v: boolean): void;
  /** The sidebar's "Research tools" group (Quant Lab, Strategy lab, AI Chat) is collapsed unless this is on. */
  showResearchTools: boolean;
  setShowResearchTools(v: boolean): void;
  /** Shared by every page with the All / DKB / Scalable filter. */
  brokerFilter: BrokerFilter;
  setBrokerFilter(v: BrokerFilter): void;
}

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function readBrokerFilter(): BrokerFilter {
  try {
    const stored = localStorage.getItem("quantfolio:brokerFilter");
    return BROKER_FILTERS.includes(stored as BrokerFilter) ? (stored as BrokerFilter) : "all";
  } catch {
    return "all";
  }
}

function writeFlag(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Storage blocked (private window): the choice lasts for this session only.
  }
}

export const useUiStore = create<UiState>((set) => ({
  viewMode: (localStorage.getItem("quantfolio:viewMode") as ViewMode | null) ?? "desktop",
  density:  (localStorage.getItem("quantfolio:density")  as Density  | null) ?? "comfortable",
  searchOpen: false,
  setViewMode(v) { localStorage.setItem("quantfolio:viewMode", v); set({ viewMode: v }); },
  setDensity(d)  { localStorage.setItem("quantfolio:density", d);  document.documentElement.dataset.density = d; set({ density: d }); },
  setSearchOpen(open) { set({ searchOpen: open }); },
  dkbUseTestPid: localStorage.getItem("quantfolio:dkbUseTestPid") === "true",
  setDkbUseTestPid(v) { localStorage.setItem("quantfolio:dkbUseTestPid", String(v)); set({ dkbUseTestPid: v }); },
  showResearchTools: readFlag("quantfolio:showResearchTools"),
  setShowResearchTools(v) { writeFlag("quantfolio:showResearchTools", v); set({ showResearchTools: v }); },
  brokerFilter: readBrokerFilter(),
  setBrokerFilter(v) {
    try {
      localStorage.setItem("quantfolio:brokerFilter", v);
    } catch {
      // Storage blocked: the filter lasts for this session only.
    }
    set({ brokerFilter: v });
  },
}));
