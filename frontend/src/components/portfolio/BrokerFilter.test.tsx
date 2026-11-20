import { describe, expect, it } from "vitest";
import { brokerOf, matchesBroker } from "./BrokerFilter";
import { heldAt } from "../../pages/portfolio/components/HoldingsTable";

describe("broker filter helpers", () => {
  it("knows the synced brokers only", () => {
    expect(brokerOf("dkb")).toBe("dkb");
    expect(brokerOf("scalable")).toBe("scalable");
    // Trading 212 positions were badged "Manual" and missing from the filter.
    expect(brokerOf("trading212")).toBe("trading212");
    expect(brokerOf("manual")).toBeNull();
    expect(brokerOf(undefined)).toBeNull();
  });

  it("keeps hand-entered positions under All only", () => {
    expect(matchesBroker("manual", "all")).toBe(true);
    expect(matchesBroker("manual", "dkb")).toBe(false);
    expect(matchesBroker("scalable", "scalable")).toBe(true);
    expect(matchesBroker("dkb", "scalable")).toBe(false);
    expect(matchesBroker("trading212", "trading212")).toBe(true);
    expect(matchesBroker("trading212", "scalable")).toBe(false);
  });
});

describe("heldAt", () => {
  const depots = new Map([["IE00B4L5Y983", ["dkb", "scalable"]], ["US1696561059", ["trading212"]]]);

  it("lists every depot behind a synced holding", () => {
    expect(heldAt({ isin: "IE00B4L5Y983", source: "dkb_sync" }, depots)).toEqual(["dkb", "scalable"]);
    expect(heldAt({ isin: "US1696561059", source: "broker_sync" }, depots)).toEqual(["trading212"]);
  });

  it("falls back to the holding's own sync source before the positions load", () => {
    // Scalable and Trading 212 share "broker_sync": no guess until a position names the depot.
    expect(heldAt({ isin: "IE00BK5BQT80", source: "broker_sync" }, depots)).toEqual([]);
    expect(heldAt({ isin: "IE00BK5BQT80", source: "dkb_sync" }, new Map())).toEqual(["dkb"]);
  });

  it("calls a hand-entered holding manual even when a depot holds the ISIN", () => {
    expect(heldAt({ isin: "IE00B4L5Y983", source: "manual" }, depots)).toEqual(["manual"]);
    expect(heldAt({ isin: "IE00B4L5Y983" }, depots)).toEqual(["manual"]);
  });
});
