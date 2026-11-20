import { describe, expect, it } from "vitest";
import { groupSlices } from "./DonutChart";

describe("groupSlices", () => {
  it("keeps the largest slices and sums the rest into Other (n)", () => {
    const slices = Array.from({ length: 70 }, (_, i) => ({ name: `T${i}`, value: i === 0 ? 900 : 1 }));
    const out = groupSlices(slices, 8);
    expect(out).toHaveLength(8);
    expect(out[0]).toEqual({ name: "T0", value: 900 });
    expect(out[7]).toEqual({ name: "Other (63)", value: 63 });
    expect(out.reduce((s, x) => s + x.value, 0)).toBe(969);
  });

  it("drops empty slices and leaves a short list alone", () => {
    expect(groupSlices([{ name: "A", value: 2 }, { name: "B", value: 0 }], 8)).toEqual([{ name: "A", value: 2 }]);
  });
});
