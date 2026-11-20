import { describe, expect, it } from "vitest";
import { sankeyNodesFromLinks } from "./SankeyChart";

describe("sankeyNodesFromLinks", () => {
  it("derives one node per distinct source/target name", () => {
    const nodes = sankeyNodesFromLinks([
      { source: "BAföG", target: "Income", value: 450 },
      { source: "Income", target: "Groceries", value: 50 },
      { source: "Groceries", target: "Rewe · Groceries", value: 50 },
    ]);

    expect(nodes).toEqual([
      { name: "BAföG" },
      { name: "Income" },
      { name: "Groceries" },
      { name: "Rewe · Groceries" },
    ]);
  });

  it("deduplicates a name that appears as both a source and a target", () => {
    const nodes = sankeyNodesFromLinks([
      { source: "Income", target: "Groceries", value: 50 },
      { source: "Groceries", target: "Rewe · Groceries", value: 30 },
      { source: "Income", target: "Eating Out", value: 20 },
    ]);

    expect(nodes).toHaveLength(4);
    expect(nodes.filter((n) => n.name === "Income")).toHaveLength(1);
  });

  it("returns an empty array for no links", () => {
    expect(sankeyNodesFromLinks([])).toEqual([]);
  });
});
