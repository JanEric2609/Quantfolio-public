import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useBlocker } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { updateSettings, type SettingsCatalogEntry, type SettingsPayload } from "../../../lib/api";
import { ATTENTION_KEY, SETTINGS_KEY } from "./schema";
import { buildDraft, buildPayload, isDirty, type Draft, type DraftValue } from "./values";

/**
 * Draft state for a set of public settings: what the inputs show, which
 * keys changed, validation errors, and the stored values to send.
 */
export function useDraft(entries: SettingsCatalogEntry[], values: SettingsPayload | undefined) {
  const [draft, setDraft] = useState<Draft>({});
  const [baseline, setBaseline] = useState<Draft>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const baselineRef = useRef<Draft>({});

  // A refetch (window focus, a save elsewhere) refreshes untouched fields
  // only; an edit in progress is never overwritten.
  useEffect(() => {
    const next = buildDraft(entries, values?.settings);
    const previous = baselineRef.current;
    setDraft((current) =>
      Object.fromEntries(entries.map((entry) => [entry.key, isDirty(entry, current, previous) ? current[entry.key] : next[entry.key]])),
    );
    baselineRef.current = next;
    setBaseline(next);
  }, [entries, values]);

  const dirtyKeys = useMemo(
    () => new Set(entries.filter((entry) => isDirty(entry, draft, baseline)).map((entry) => entry.key)),
    [entries, draft, baseline],
  );

  const setValue = useCallback((key: string, value: DraftValue) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setErrors((current) => {
      if (!(key in current)) return current;
      const { [key]: _removed, ...rest } = current;
      return rest;
    });
  }, []);

  /** Changed values ready to store, or null after flagging invalid fields. */
  const collect = useCallback((): Record<string, unknown> | null => {
    const { settings, errors: nextErrors } = buildPayload(entries, draft, baseline);
    setErrors(nextErrors);
    const firstError = Object.keys(nextErrors)[0];
    if (firstError) {
      document.getElementById(`setting-${firstError}`)?.focus();
      return null;
    }
    return settings;
  }, [entries, draft, baseline]);

  const discard = useCallback(() => {
    setDraft(baseline);
    setErrors({});
  }, [baseline]);

  return { draft, dirtyKeys, errors, setValue, collect, discard };
}

/** Confirm before leaving the page (in-app navigation or tab close) with unsaved changes. */
export function useLeaveGuard(dirty: boolean) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);

  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("You have unsaved changes. Leave without saving?")) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
}

export function useSaveSettings(label: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: { settings?: Record<string, unknown>; integrations?: Record<string, Record<string, unknown>> }) =>
      updateSettings(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: SETTINGS_KEY, exact: true });
      queryClient.invalidateQueries({ queryKey: ATTENTION_KEY });
      toast.success(`${label} saved`);
    },
    onError: (error: Error) => toast.error(`Save failed: ${error.message}`),
  });
}
