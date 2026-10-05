import type { ConnectionTestResult, SettingsCatalogEntry, SettingsConnection, SettingsPayload } from "../../../lib/api";

export type ConnectionState = "working" | "failing" | "untested" | "missing" | "off";

export type ConnectionStatus = {
  state: ConnectionState;
  label: string;
  lastTest: ConnectionTestResult | null;
};

/**
 * A connection's status from what is actually stored: its credential, its
 * on/off switch, its required settings (every text or number setting not on
 * `optional_settings`) and the last recorded test.
 */
export function connectionStatus(
  connection: SettingsConnection,
  values: SettingsPayload | undefined,
  catalog: Record<string, SettingsCatalogEntry>,
): ConnectionStatus {
  const settings = values?.settings ?? {};
  const lastTest = values?.integrations?.[connection.id]?.last_test ?? null;

  if (connection.enabled_key && !settings[connection.enabled_key]) {
    return { state: "off", label: "Turned off", lastTest };
  }

  const hasCredential = connection.secret ? !!values?.integrations?.[connection.secret]?.configured : false;
  const optional = new Set(connection.optional_settings ?? []);
  const textSettingsFilled = connection.settings
    .filter((key) => !optional.has(key))
    .map((key) => catalog[key])
    .filter((entry) => entry && ["text", "number"].includes(entry.input_type))
    .every((entry) => {
      const value = settings[entry.key] ?? entry.default;
      return value !== null && value !== undefined && String(value).trim() !== "";
    });
  const configured = connection.secret && !connection.secret_optional ? hasCredential : textSettingsFilled;

  if (!configured) return { state: "missing", label: "Not set up", lastTest };
  if (lastTest) {
    return lastTest.ok
      ? { state: "working", label: "Working", lastTest }
      : { state: "failing", label: "Test failed", lastTest };
  }
  return { state: "untested", label: connection.testable ? "Set up · not tested" : "Set up", lastTest };
}

export const STATE_DOT: Record<ConnectionState, string> = {
  working: "bg-success",
  failing: "bg-danger",
  untested: "bg-info",
  missing: "bg-text-muted/50",
  off: "bg-text-muted/50",
};
