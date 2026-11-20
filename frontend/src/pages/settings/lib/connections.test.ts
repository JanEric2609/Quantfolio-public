import { describe, expect, it } from "vitest";
import type { SettingsCatalogEntry, SettingsConnection, SettingsPayload } from "../../../lib/api";
import { connectionStatus } from "./connections";

function entry(key: string, overrides: Partial<SettingsCatalogEntry> = {}): SettingsCatalogEntry {
  return {
    key,
    group: "banking",
    label: key,
    help: "Help",
    input_type: "text",
    sensitive: false,
    status: "active",
    connection: "scalable",
    ...overrides,
  };
}

const catalog: Record<string, SettingsCatalogEntry> = {
  scalable_enabled: entry("scalable_enabled", { input_type: "boolean" }),
  scalable_portfolio_id: entry("scalable_portfolio_id", { default: "" }),
  scalable_wrapper_path: entry("scalable_wrapper_path", { default: "/usr/local/libexec/quantfolio-sc-ro" }),
  scalable_sync_hours: entry("scalable_sync_hours", { input_type: "number", default: 6 }),
};

const scalable: SettingsConnection = {
  id: "scalable",
  group: "banking",
  label: "Scalable Capital (sc CLI)",
  description: "",
  secret: null,
  secret_label: "API key",
  secret_optional: false,
  meta_fields: [],
  enabled_key: "scalable_enabled",
  testable: true,
  panel: "scalable_login",
  optional_settings: ["scalable_portfolio_id"],
  settings: Object.keys(catalog),
};

function values(settings: Record<string, unknown>, lastTest?: { ok: boolean }): SettingsPayload {
  return {
    settings,
    integrations: lastTest ? { scalable: { configured: false, last_test: lastTest } } : {},
  } as unknown as SettingsPayload;
}

describe("connectionStatus", () => {
  it("counts a switched-on connection as set up while an optional setting is empty", () => {
    const status = connectionStatus(scalable, values({ scalable_enabled: true, scalable_portfolio_id: "" }), catalog);
    expect(status.state).toBe("untested");
    expect(status.label).toBe("Set up · not tested");
  });

  it("shows the last test once an optional setting is the only empty one", () => {
    const passed = connectionStatus(scalable, values({ scalable_enabled: true }, { ok: true }), catalog);
    expect(passed.label).toBe("Working");
    const failed = connectionStatus(scalable, values({ scalable_enabled: true }, { ok: false }), catalog);
    expect(failed.label).toBe("Test failed");
  });

  it("still needs every setting that is not optional", () => {
    const status = connectionStatus(scalable, values({ scalable_enabled: true, scalable_wrapper_path: " " }), catalog);
    expect(status.label).toBe("Not set up");
  });

  it("treats every text setting as required when the connection lists none as optional", () => {
    const strict = { ...scalable, optional_settings: undefined };
    expect(connectionStatus(strict, values({ scalable_enabled: true }), catalog).label).toBe("Not set up");
  });

  it("says turned off before anything else", () => {
    expect(connectionStatus(scalable, values({ scalable_enabled: false }), catalog).label).toBe("Turned off");
  });
});
