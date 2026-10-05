import { describe, expect, it } from "vitest";
import { responsiveChartHeight } from "./EChart";

describe("responsiveChartHeight", () => {
  it("shrinks tall charts with the viewport width but never below 60 % of the nominal height", () => {
    expect(responsiveChartHeight(360)).toBe("min(360px, max(216px, 75vw))");
    expect(responsiveChartHeight(320)).toBe("min(320px, max(192px, 75vw))");
  });

  it("leaves sparklines and other short charts alone", () => {
    expect(responsiveChartHeight(32)).toBe("32px");
    expect(responsiveChartHeight(120)).toBe("120px");
  });

  it("passes explicit CSS heights through", () => {
    expect(responsiveChartHeight("18rem")).toBe("18rem");
  });
});
