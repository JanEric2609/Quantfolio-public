import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { ExternalLink, Loader2, PlugZap } from "lucide-react";
import { toast } from "sonner";
import {
  testSettingsIntegration,
  type SettingsCatalogEntry,
  type SettingsConnection,
  type SettingsPayload,
} from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../../../components/ui/sheet";
import { cn } from "../../../lib/utils";
import { connectionStatus, STATE_DOT } from "../lib/connections";
import { SETTINGS_KEY } from "../lib/schema";
import { useDraft, useSaveSettings } from "../lib/useSettingsForm";
import { MaskedInput } from "./MaskedInput";
import { SettingRow } from "./SettingRow";
import { ScalableLoginCard } from "./ScalableLoginCard";
import { TelegramPairingCard } from "./TelegramPairingCard";

/** Meta that the settings API echoes as a public value, so the drawer can prefill it. */
const PREFILLED_META: Record<string, Record<string, string>> = {
  dkb: { username: "dkb_username" },
};

export function ConnectionDrawer({
  connection,
  catalog,
  values,
  open,
  onOpenChange,
}: {
  connection: SettingsConnection;
  catalog: Record<string, SettingsCatalogEntry>;
  values: SettingsPayload | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const entries = useMemo(
    () => connection.settings.map((key) => catalog[key]).filter((entry): entry is SettingsCatalogEntry => !!entry && !entry.sensitive),
    [connection.settings, catalog],
  );
  // The on/off switch leads, everything else keeps catalog order.
  const ordered = useMemo(
    () => [...entries].sort((a, b) => Number(b.key === connection.enabled_key) - Number(a.key === connection.enabled_key)),
    [entries, connection.enabled_key],
  );
  const form = useDraft(entries, values);
  const secretEntry = connection.secret ? catalog[connection.secret] : undefined;
  const credentialStored = connection.secret ? !!values?.integrations?.[connection.secret]?.configured : false;
  const status = connectionStatus(connection, values, catalog);

  const prefilled = useMemo(() => {
    const out: Record<string, string> = {};
    for (const [metaKey, settingKey] of Object.entries(PREFILLED_META[connection.id] ?? {})) {
      out[metaKey] = String(values?.settings?.[settingKey] ?? "");
    }
    return out;
  }, [connection.id, values]);

  const [secret, setSecret] = useState("");
  const [meta, setMeta] = useState<Record<string, string>>({});
  const [credentialError, setCredentialError] = useState<string>();

  useEffect(() => {
    if (!open) return;
    setSecret("");
    setMeta(prefilled);
    setCredentialError(undefined);
  }, [open, prefilled]);

  const metaChanges = Object.fromEntries(
    connection.meta_fields
      .filter((field) => (meta[field.key] ?? "") !== (prefilled[field.key] ?? ""))
      .map((field) => [field.key, meta[field.key] ?? ""]),
  );
  const dirty = form.dirtyKeys.size > 0 || secret !== "" || Object.keys(metaChanges).length > 0;

  const save = useSaveSettings(connection.label);
  const test = useMutation({
    mutationFn: () => testSettingsIntegration({ service: connection.id }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: SETTINGS_KEY, exact: true });
      if (result.ok) toast.success(`${connection.label}: ${result.message}`);
      else toast.error(`${connection.label}: ${result.message}`);
    },
    onError: (error: Error) => toast.error(`Test failed: ${error.message}`),
  });

  const persist = async () => {
    const settings = form.collect();
    if (settings === null) return false;
    if (connection.secret && !credentialStored && !connection.secret_optional && !secret) {
      setCredentialError(`Enter the ${secretEntry?.label ?? connection.secret_label} to connect.`);
      document.getElementById(`secret-${connection.id}`)?.focus();
      return false;
    }
    const integrations: Record<string, Record<string, unknown>> = {};
    if (connection.secret && (secret || Object.keys(metaChanges).length)) {
      integrations[connection.secret] = { ...(secret ? { secret } : {}), ...metaChanges };
    }
    if (!Object.keys(settings).length && !Object.keys(integrations).length) return true;
    await save.mutateAsync({ settings, integrations });
    setSecret("");
    return true;
  };

  const close = (next: boolean) => {
    if (!next && dirty && !window.confirm(`Discard unsaved changes to ${connection.label}?`)) return;
    if (!next) form.discard();
    onOpenChange(next);
  };

  const busy = save.isPending || test.isPending;
  const setupSteps = secretEntry?.setup_steps ?? [];
  const docUrl = [secretEntry?.doc_url, ...entries.map((entry) => entry.doc_url)].find((url) => url && /^https?:\/\//i.test(url));

  return (
    <Sheet open={open} onOpenChange={close}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-lg">
        <SheetHeader className="border-b border-border px-6 py-5">
          <SheetTitle>{connection.label}</SheetTitle>
          <SheetDescription>{connection.description}</SheetDescription>
          <div className="flex flex-wrap items-center gap-2 pt-1 text-sm">
            <span className={cn("h-2 w-2 rounded-full", STATE_DOT[status.state])} aria-hidden="true" />
            <span className="font-medium">{status.label}</span>
            {status.lastTest ? (
              <span className="text-text-muted">
                · tested {formatDistanceToNow(new Date(status.lastTest.tested_at), { addSuffix: true })}
              </span>
            ) : null}
          </div>
          {status.lastTest && !status.lastTest.ok ? (
            <p role="alert" className="rounded-md border border-danger/30 bg-danger/10 p-2 text-xs text-danger">
              {status.lastTest.message}
            </p>
          ) : null}
        </SheetHeader>

        <form
          id={`connection-${connection.id}`}
          className="flex-1 divide-y divide-border overflow-y-auto px-6"
          onSubmit={async (event) => {
            event.preventDefault();
            await persist();
          }}
        >
          {ordered.map((entry) => (
            <SettingRow
              key={entry.key}
              entry={entry}
              stacked
              value={form.draft[entry.key]}
              dirty={form.dirtyKeys.has(entry.key)}
              error={form.errors[entry.key]}
              onChange={(value) => form.setValue(entry.key, value)}
            />
          ))}

          {connection.meta_fields.filter((field) => !field.sensitive).map((field) => (
            <div key={field.key} className="space-y-2 py-4">
              <label htmlFor={`meta-${connection.id}-${field.key}`} className="text-sm font-medium">
                {field.label}
              </label>
              {field.sensitive ? (
                <MaskedInput
                  id={`meta-${connection.id}-${field.key}`}
                  value={meta[field.key] ?? ""}
                  onChange={(value) => setMeta((current) => ({ ...current, [field.key]: value }))}
                  configured={credentialStored}
                  placeholder={field.placeholder ?? `Enter ${field.label.toLowerCase()}`}
                  ariaLabel={field.label}
                />
              ) : (
                <Input
                  id={`meta-${connection.id}-${field.key}`}
                  value={meta[field.key] ?? ""}
                  onChange={(event) => setMeta((current) => ({ ...current, [field.key]: event.target.value }))}
                  placeholder={field.placeholder ?? undefined}
                  autoComplete="off"
                />
              )}
            </div>
          ))}

          {secretEntry ? (
            <div className="space-y-2 py-4">
              <div className="flex items-center gap-2">
                <label htmlFor={`secret-${connection.id}`} className="text-sm font-medium">
                  {connection.secret_label}
                </label>
                {connection.secret_optional ? <span className="text-xs text-text-muted">optional</span> : null}
              </div>
              <p className="text-sm text-text-secondary">{secretEntry.help}</p>
              <MaskedInput
                id={`secret-${connection.id}`}
                value={secret}
                onChange={(value) => {
                  setSecret(value);
                  setCredentialError(undefined);
                }}
                configured={credentialStored}
                placeholder={`Enter ${connection.secret_label.toLowerCase()}`}
                ariaLabel={connection.secret_label}
              />
              {credentialError ? (
                <p role="alert" className="text-sm text-danger">
                  {credentialError}
                </p>
              ) : null}
              <p className="text-xs text-text-muted">Stored encrypted. It is never shown again after saving.</p>
            </div>
          ) : null}

          {connection.meta_fields.filter((field) => field.sensitive).map((field) => (
            <div key={field.key} className="space-y-2 py-4">
              <label htmlFor={`meta-${connection.id}-${field.key}`} className="text-sm font-medium">
                {field.label}
              </label>
              {field.sensitive ? (
                <MaskedInput
                  id={`meta-${connection.id}-${field.key}`}
                  value={meta[field.key] ?? ""}
                  onChange={(value) => setMeta((current) => ({ ...current, [field.key]: value }))}
                  configured={credentialStored}
                  placeholder={field.placeholder ?? `Enter ${field.label.toLowerCase()}`}
                  ariaLabel={field.label}
                />
              ) : (
                <Input
                  id={`meta-${connection.id}-${field.key}`}
                  value={meta[field.key] ?? ""}
                  onChange={(event) => setMeta((current) => ({ ...current, [field.key]: event.target.value }))}
                  placeholder={field.placeholder ?? undefined}
                  autoComplete="off"
                />
              )}
            </div>
          ))}

          {connection.panel === "scalable_login" ? (
            <div className="py-4">
              <ScalableLoginCard />
            </div>
          ) : null}

          {connection.panel === "telegram_pairing" && credentialStored ? (
            <div className="py-4">
              <TelegramPairingCard />
            </div>
          ) : null}

          {setupSteps.length || docUrl ? (
            <details className="group py-4" open={!credentialStored && status.state === "missing"}>
              <summary className="cursor-pointer text-sm font-medium text-text-secondary hover:text-text-primary">
                How to set up
              </summary>
              {setupSteps.length ? (
                <ol className="mt-2 list-inside list-decimal space-y-1 text-sm text-text-secondary">
                  {setupSteps.map((step) => (
                    <li key={step}>{step}</li>
                  ))}
                </ol>
              ) : null}
              {docUrl ? (
                <a href={docUrl} target="_blank" rel="noreferrer" className="mt-2 inline-flex items-center gap-1 text-sm text-accent hover:underline">
                  Provider documentation <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                </a>
              ) : null}
            </details>
          ) : null}
        </form>

        <div className="flex items-center justify-between gap-2 border-t border-border bg-surface px-6 py-4">
          {connection.testable ? (
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={async () => {
                if (dirty && !(await persist())) return;
                test.mutate();
              }}
            >
              {test.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : <PlugZap className="mr-2 h-4 w-4" aria-hidden="true" />}
              {dirty ? "Save & test" : "Test connection"}
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            <Button type="button" variant="ghost" onClick={() => close(false)} disabled={busy}>
              {dirty ? "Cancel" : "Close"}
            </Button>
            <Button type="submit" form={`connection-${connection.id}`} disabled={busy || !dirty}>
              {save.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : null}
              Save
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
