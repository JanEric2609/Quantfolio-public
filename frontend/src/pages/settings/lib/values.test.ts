import { describe, expect, it } from "vitest";
import type { SettingsCatalogEntry } from "../../../lib/api";
import { buildPayload, differsFromDefault, formatValue, fromDraft, toDraft } from "./values";

function entry(overrides: Partial<SettingsCatalogEntry>): SettingsCatalogEntry {
  return {
    key: "k",
    group: "profile",
    label: "Label",
    help: "Help",
    input_type: "text",
    sensitive: false,
    status: "active",
    ...overrides,
  };
}

describe("fraction_pct", () => {
  const threshold = entry({ input_type: "number", unit: "fraction_pct", max: 0 });

  it("shows fractions as percent without float noise", () => {
    expect(toDraft(threshold, -0.05)).toBe("-5");
    expect(toDraft(threshold, 0.3)).toBe("30");
  });

  it("stores percent input as a fraction and checks bounds in stored units", () => {
    expect(fromDraft(threshold, "-7.5")).toEqual({ ok: true, value: -0.075 });
    expect(fromDraft(threshold, "-7,5")).toEqual({ ok: true, value: -0.075 });
    expect(fromDraft(threshold, "5")).toEqual({ ok: false, error: "At most 0 %" });
  });
});

describe("json settings", () => {
  const overrides = entry({ input_type: "json" });

  it("round-trips an object instead of rendering [object Object]", () => {
    const shown = toDraft(overrides, { IE00B4L5Y983: "EUNL.DE" });
    expect(shown).toContain('"IE00B4L5Y983": "EUNL.DE"');
    expect(fromDraft(overrides, shown)).toEqual({ ok: true, value: { IE00B4L5Y983: "EUNL.DE" } });
  });

  it("rejects arrays and invalid JSON", () => {
    expect(fromDraft(overrides, "[1]").ok).toBe(false);
    expect(fromDraft(overrides, "{nope").ok).toBe(false);
  });
});

describe("buildPayload", () => {
  const entries = [
    entry({ key: "monthly_contribution_eur", input_type: "number", min: 0 }),
    entry({ key: "passive_core_ticker" }),
    entry({ key: "untouched" }),
    entry({ key: "secret", sensitive: true, input_type: "password" }),
  ];

  it("sends only changed keys, clears to null and reports invalid values", () => {
    const baseline = { monthly_contribution_eur: "100", passive_core_ticker: "EUNL.DE", untouched: "x", secret: "" };
    const draft = { monthly_contribution_eur: "-1", passive_core_ticker: "", untouched: "x", secret: "typed" };

    expect(buildPayload(entries, draft, baseline)).toEqual({
      settings: { passive_core_ticker: null },
      errors: { monthly_contribution_eur: "At least 0" },
    });
  });
});

describe("display helpers", () => {
  it("labels select options and units", () => {
    const broker = entry({ input_type: "select", options: ["dkb"], option_labels: { dkb: "DKB" } });
    const days = entry({ input_type: "number", unit: "days" });
    expect(formatValue(broker, "dkb")).toBe("DKB");
    expect(formatValue(days, 30)).toBe("30 days");
  });

  it("compares numbers and objects to the default by value", () => {
    expect(differsFromDefault(entry({ input_type: "number", default: 15 }), 15.0)).toBe(false);
    expect(differsFromDefault(entry({ input_type: "json", default: {} }), {})).toBe(false);
    expect(differsFromDefault(entry({ default: "EUNL.DE" }), "IWDA.AS")).toBe(true);
  });
});
