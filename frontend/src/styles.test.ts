import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

// jsdom does not evaluate the cascade (vitest runs with css: false), so these
// guard the two config mistakes that broke styling in the browser by reading the sources.
describe("global styles", () => {
  it("does not reset font/colour on form controls outside a layer (it beats every Tailwind utility)", () => {
    const css = read("./styles.css")
      .replace(/\/\*[\s\S]*?\*\//g, "") // comments may mention the selector
      .replace(/@media[^{]*\{[\s\S]*?\}\s*\}/g, ""); // the phone font-size guard is intentionally unlayered
    expect(css).not.toMatch(/(^|\n)\s*button\s*,\s*input\s*,\s*select\s*,\s*textarea\s*\{[^}]*(font|color)\s*:/);
  });

  it("keeps the 16 px phone guard for form fields (iOS focus zoom)", () => {
    expect(read("./styles.css")).toMatch(/max-width:\s*639\.98px[\s\S]*font-size:\s*16px/);
  });
});

describe("tailwind theme", () => {
  it("has no `base` colour alias: it turns `text-base` into a colour instead of a font size", () => {
    const config = read("../tailwind.config.js").replace(/\/\/.*$/gm, "");
    expect(config).not.toMatch(/^\s*base\s*:/m);
  });
});
