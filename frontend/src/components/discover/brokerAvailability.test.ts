import { describe, expect, it } from "vitest";
import { brokerAvailability } from "./brokerAvailability";

describe("brokerAvailability", () => {
  it("adds a Trading 212 row once the run checked it", () => {
    const rows = brokerAvailability({
      brokers: {
        dkb: { likely: true, confidence: "medium", url: "https://dkb" },
        scalable: { likely: true, confidence: "medium", url: "https://scalable" },
        trading212: { likely: false, confidence: "high", note: "Not in Trading 212's instrument list", url: null },
      },
    });
    expect(rows.map((row) => row.label)).toEqual(["DKB", "Scalable", "Trading 212"]);
    expect(rows[2]).toMatchObject({ key: "trading212", likely: false, url: null });
  });

  it("shows no Trading 212 row for a run that did not check it", () => {
    const rows = brokerAvailability({ brokers: { dkb: { likely: true }, scalable: { likely: true } } });
    expect(rows.map((row) => row.key)).toEqual(["dkb", "scalable"]);
  });
});
