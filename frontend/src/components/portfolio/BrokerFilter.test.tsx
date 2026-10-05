import { describe, expect, it } from "vitest";
import { brokerOf, matchesBroker } from "./BrokerFilter";
import { heldAt } from "../../pages/portfolio/components/HoldingsTable";

describe("broker filter helpers", () => {
  it("knows the synced brokers only", () => {
    expect(brokerOf("dkb")).toBe("dkb");
    expect(brokerOf("scalable")).toBe("scalable");
    expect(brokerOf("manual")).toBeNull();
    expect(brokerOf(undefined)).toBeNull();
  });

  it("keeps hand-entered positions under All only", () => {
    expect(matchesBroker("manual", "all")).toBe(true);
    expect(matchesBroker("manual", "dkb")).toBe(false);
    expect(matchesBroker("scalable", "scalable")).toBe(true);
    expect(matchesBroker("dkb", "scalable")).toBe(false);
  });
});

describe("heldAt", () => {
  const depots = new Map([["IE00B4L5Y983", ["dkb", "scalable"]]]);

  it("lists every depot behind a synced holding", () => {
    expect(heldAt({ isin: "IE00B4L5Y983", source: "dkb_sync" }, depots)).toEqual(["dkb", "scalable"]);
  });

  it("falls back to the holding's own sync source before the positions load", () => {
    expect(heldAt({ isin: "IE00BK5BQT80", source: "broker_sync" }, depots)).toEqual(["scalable"]);
    expect(heldAt({ isin: "IE00BK5BQT80", source: "dkb_sync" }, new Map())).toEqual(["dkb"]);
  });

  it("calls a hand-entered holding manual even when a depot holds the ISIN", () => {
    expect(heldAt({ isin: "IE00B4L5Y983", source: "manual" }, depots)).toEqual(["manual"]);
    expect(heldAt({ isin: "IE00B4L5Y983" }, depots)).toEqual(["manual"]);
  });
});
