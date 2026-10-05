/**
 * The Strategy lab (report Phase 5): one place for everything that tests a
 * strategy before it may touch money, the paper portfolio included. Each tab
 * keeps its own route, so old links and bookmarks keep working.
 */
export interface StrategyLabTab {
  to: string;
  label: string;
  /** What the tab means for real money, in one line. */
  consequence: string;
}

export const STRATEGY_LAB_TABS: StrategyLabTab[] = [
  {
    to: "/evidence",
    label: "Evidence",
    consequence: "A passing check here is the only thing that changes This month's plan.",
  },
  {
    to: "/advisor",
    label: "LLM advisor",
    consequence: "Paper only. Its picks never set weights for your real book.",
  },
  {
    to: "/mandates",
    label: "Mandates",
    consequence: "Paper only. A comparison of rule sets, not advice.",
  },
  {
    to: "/graduation",
    label: "Graduation",
    consequence: "Tracks the LLM advisor's paper record. Graduating no longer unlocks real-money advice.",
  },
  {
    // Was a Portfolio tab; the URL is unchanged.
    to: "/portfolio/paper",
    label: "Paper portfolio",
    consequence: "Paper only. A simulated book for testing ideas; it never touches your real holdings.",
  },
];

export function isStrategyLabPath(pathname: string): boolean {
  return STRATEGY_LAB_TABS.some((tab) => pathname === tab.to || pathname.startsWith(`${tab.to}/`));
}

export function activeStrategyLabTab(pathname: string): StrategyLabTab | undefined {
  return STRATEGY_LAB_TABS.find((tab) => pathname === tab.to || pathname.startsWith(`${tab.to}/`));
}
