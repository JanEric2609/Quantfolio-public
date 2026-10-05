import { describe, expect, it } from "vitest";
import {
  EXTRA_PAGES,
  ROUTE_ENTRIES,
  activeEntry,
  breadcrumbFor,
  isResearchToolPath,
  mobileSlots,
  paletteEntries,
  sidebarGroups,
  titleForPath,
} from "./routeManifest";

describe("route manifest", () => {
  it("lists eleven primary sidebar items plus Control Center in the footer", () => {
    const groups = sidebarGroups();
    const main = groups.filter((g) => g.id !== "admin").flatMap((g) => g.items);

    expect(main.map((e) => e.label)).toEqual([
      "Home", "This month", "Portfolio", "Tax", "Budget", "Discover", "Watchlist", "News",
      "Can I trust it?", "Quant Lab", "Strategy lab", "AI Chat",
    ]);
    expect(groups.find((g) => g.id === "admin")?.items.map((e) => e.label)).toEqual(["Control Center"]);
    expect(groups.map((g) => g.label)).toEqual(["Today", "My money", "Ideas", "Trust", "Research tools", "Admin"]);
  });

  it("flags exactly Quant Lab, Strategy lab and AI Chat as research tools", () => {
    expect(ROUTE_ENTRIES.filter((e) => e.researchTool).map((e) => e.label)).toEqual(["Quant Lab", "Strategy lab", "AI Chat"]);
    expect(isResearchToolPath("/quantlab/risk")).toBe(true);
    expect(isResearchToolPath("/advisor/loop")).toBe(true);
    expect(isResearchToolPath("/portfolio/paper")).toBe(true);
    expect(isResearchToolPath("/portfolio/holdings")).toBe(false);
  });

  it("gives the bottom nav four routed slots in order", () => {
    expect(mobileSlots().map((e) => [e.mobile.slot, e.mobile.label, e.to])).toEqual([
      [1, "Plan", "/plan"],
      [2, "Overview", "/"],
      [3, "Holdings", "/portfolio/holdings"],
      [4, "Decide", "/decide"],
    ]);
  });

  it("keeps Decide out of the sidebar but reachable", () => {
    expect(sidebarGroups().flatMap((g) => g.items).some((e) => e.id === "decide")).toBe(false);
    expect(paletteEntries().some((e) => e.to === "/decide")).toBe(true);
  });

  it("never assigns one path to two entries", () => {
    const paths = [...ROUTE_ENTRIES.map((e) => e.to), ...EXTRA_PAGES.map((p) => p.to)];
    // The detailed overview is an extra page of Home, not a second entry.
    expect(new Set(paths).size).toBe(paths.length);
    expect(new Set(paletteEntries().map((e) => e.label)).size).toBe(paletteEntries().length);
  });

  it("resolves a path to exactly one entry", () => {
    for (const [path, id] of [
      ["/", "home"], ["/dashboard", "home"], ["/plan", "plan"], ["/portfolio/risk", "portfolio"],
      ["/portfolio/analysis", "portfolio"], ["/portfolio/paper", "strategy-lab"], ["/tax/lots", "tax"],
      ["/money/subscriptions", "budget"], ["/research/stocks/AAPL", "watchlist"], ["/news", "news"],
      ["/discover", "discover"], ["/trust", "trust"], ["/evidence", "strategy-lab"], ["/chat", "chat"],
      ["/settings/status", "settings"], ["/decide", "decide"],
    ] as const) {
      expect(activeEntry(path)?.id, path).toBe(id);
    }
    expect(activeEntry("/imports")).toBeUndefined();
  });

  it("builds titles and breadcrumbs from the manifest", () => {
    expect(titleForPath("/tax/overview")).toBe("Tax");
    expect(titleForPath("/imports")).toBeUndefined();
    expect(breadcrumbFor("/portfolio/holdings")).toEqual([{ label: "My money" }, { label: "Portfolio" }]);
    expect(breadcrumbFor("/settings")).toEqual([{ label: "Control Center" }]);
    expect(breadcrumbFor("/imports")).toEqual([]);
  });
});
