import { describe, expect, it } from "vitest";
import { mapLimit } from "./mapLimit";

describe("mapLimit", () => {
  it("keeps the order and never runs more than the limit at once", async () => {
    let running = 0;
    let peak = 0;
    const out = await mapLimit([1, 2, 3, 4, 5, 6, 7], 3, async (n) => {
      running += 1;
      peak = Math.max(peak, running);
      await new Promise((resolve) => setTimeout(resolve, 8 - n));
      running -= 1;
      return n * 2;
    });

    expect(out).toEqual([2, 4, 6, 8, 10, 12, 14]);
    expect(peak).toBe(3);
  });

  it("rejects when a call fails and copes with nothing to do", async () => {
    await expect(mapLimit([1], 2, async () => { throw new Error("down"); })).rejects.toThrow("down");
    await expect(mapLimit([], 2, async (n: number) => n)).resolves.toEqual([]);
  });
});
