import { afterEach, expect, it } from "vitest";
import { token } from "./tokens";

afterEach(() => document.documentElement.style.removeProperty("--c-success"));

it("falls back to the styles.css value when the variable is not set", () => {
  expect(token("success")).toBe("rgb(34,197,94)");
  expect(token("danger", 0.3)).toBe("rgba(239,68,68,0.3)");
});

it("reads the variable the page sets, so a chart follows the theme", () => {
  document.documentElement.style.setProperty("--c-success", "1 2 3");
  expect(token("success")).toBe("rgb(1,2,3)");
});
