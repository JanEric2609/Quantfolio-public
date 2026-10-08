import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type TaxSettings } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../../components/ui/card";

type Tri = "unknown" | "yes" | "no";
const toTri = (v: boolean | null | undefined): Tri => (v == null ? "unknown" : v ? "yes" : "no");
const fromTri = (v: Tri): boolean | null => (v === "unknown" ? null : v === "yes");
const num = (v: string): number | null => {
  const n = Number.parseFloat(v.replace(",", "."));
  return v.trim() === "" || !Number.isFinite(n) ? null : n;
};

const BANKS = [
  { key: "dkb", label: "DKB" },
  { key: "scalable", label: "Scalable Capital" },
] as const;

const fieldClass = "rounded border border-border bg-surface px-2 py-1 text-sm";

/**
 * One place for what each bank knows about you: an NV certificate copy or a
 * Freistellungsauftrag, plus the facts that size the tax-free room. Saves
 * through PUT /api/tax/settings, which rejects orders above the allowance
 * and an NV end date that is not a 31 December.
 */
export function TaxStatusCard() {
  const qc = useQueryClient();
  // Same key and shape as TaxCockpitLayout: the cache holds the unwrapped settings.
  const settings = useQuery({
    queryKey: ["tax", "settings"],
    queryFn: () => api<{ settings: TaxSettings }>("/api/tax/settings").then((r) => r.settings),
  });
  const [draft, setDraft] = useState<TaxSettings>({});
  useEffect(() => {
    if (settings.data) setDraft(settings.data);
  }, [settings.data]);

  const save = useMutation({
    mutationFn: (body: TaxSettings) =>
      api<{ settings: TaxSettings }>("/api/tax/settings", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => {
      toast.success("Tax status saved.");
      qc.invalidateQueries({ queryKey: ["tax"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Could not save."),
  });

  const set = (patch: TaxSettings) => setDraft((d) => ({ ...d, ...patch }));
  const allowance = draft.tax_spouse_allowance ? 2000 : 1000;
  const orders =
    (draft.freistellungsauftrag_dkb_eur ?? 0) +
    (draft.freistellungsauftrag_scalable_eur ?? 0) +
    (draft.freistellungsauftrag_other_banks_eur ?? 0);
  const over = orders > allowance;
  const nv = draft.tax_nv_certificate === true;
  const until = draft.tax_nv_valid_until ?? "";
  const badUntil = nv && until !== "" && !until.endsWith("-12-31");

  if (settings.isLoading) return <div className="text-sm text-text-secondary">Loading tax status…</div>;

  return (
    <Card className="border-border bg-surface" data-testid="tax-status-card">
      <CardHeader>
        <CardTitle>Your tax status</CardTitle>
        <CardDescription>
          Each bank applies only what it has on file: a copy of your NV certificate, or a Freistellungsauftrag.
          Together the orders may not exceed {allowance.toLocaleString("de-DE")} €.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5 text-sm">
        <fieldset className="space-y-2">
          <legend className="font-medium text-text-primary">NV certificate (Nichtveranlagungsbescheinigung)</legend>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={nv}
              onChange={(e) => set({ tax_nv_certificate: e.target.checked })}
              aria-label="I have an NV certificate"
            />
            I have one from the Finanzamt
          </label>
          {nv && (
            <label className="flex flex-wrap items-center gap-2">
              <span className="text-text-secondary">Valid until</span>
              <input
                type="date"
                className={fieldClass}
                value={until}
                onChange={(e) => set({ tax_nv_valid_until: e.target.value })}
                aria-label="NV certificate valid until"
              />
              {badUntil && <span className="text-xs text-danger">An NV certificate always ends on 31 December.</span>}
            </label>
          )}
        </fieldset>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-text-secondary">
              <tr className="border-b border-border">
                <th className="py-1.5 pr-3 font-medium">Bank</th>
                {nv && <th className="py-1.5 pr-3 font-medium">NV copy on file</th>}
                <th className="py-1.5 pr-3 font-medium">Freistellungsauftrag (€)</th>
              </tr>
            </thead>
            <tbody>
              {BANKS.map((b) => {
                const filedKey = `tax_nv_filed_${b.key}` as const;
                const fsaKey = `freistellungsauftrag_${b.key}_eur` as const;
                return (
                  <tr key={b.key} className="border-b border-border/60">
                    <td className="py-1.5 pr-3">{b.label}</td>
                    {nv && (
                      <td className="py-1.5 pr-3">
                        <select
                          className={fieldClass}
                          value={toTri(draft[filedKey])}
                          onChange={(e) => set({ [filedKey]: fromTri(e.target.value as Tri) })}
                          aria-label={`NV copy at ${b.label}`}
                        >
                          <option value="unknown">Not answered (assumed yes)</option>
                          <option value="yes">Yes</option>
                          <option value="no">No</option>
                        </select>
                      </td>
                    )}
                    <td className="py-1.5 pr-3">
                      <input
                        inputMode="decimal"
                        className={`${fieldClass} w-28`}
                        value={draft[fsaKey] ?? ""}
                        placeholder="none"
                        onChange={(e) => set({ [fsaKey]: num(e.target.value) })}
                        aria-label={`Freistellungsauftrag at ${b.label}`}
                      />
                    </td>
                  </tr>
                );
              })}
              <tr>
                <td className="py-1.5 pr-3">Other banks</td>
                {nv && <td className="py-1.5 pr-3 text-text-muted">—</td>}
                <td className="py-1.5 pr-3">
                  <input
                    inputMode="decimal"
                    className={`${fieldClass} w-28`}
                    value={draft.freistellungsauftrag_other_banks_eur ?? ""}
                    placeholder="none"
                    onChange={(e) => set({ freistellungsauftrag_other_banks_eur: num(e.target.value) })}
                    aria-label="Freistellungsaufträge at other banks"
                  />
                </td>
              </tr>
            </tbody>
          </table>
          {over && (
            <p className="mt-1 text-xs text-danger">
              The orders add up to {orders.toLocaleString("de-DE")} €, more than the {allowance.toLocaleString("de-DE")} € allowance.
            </p>
          )}
          {nv && (
            <p className="mt-1 text-xs text-text-muted">
              A bank with an NV copy withholds nothing, so its Freistellungsauftrag sits idle there; give it to the
              bank without a copy instead.
            </p>
          )}
        </div>

        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <legend className="mb-1 font-medium text-text-primary">Your situation</legend>
          <label className="flex flex-col gap-1">
            <span className="text-text-secondary">Other income this year (€, after Werbungskosten)</span>
            <input
              inputMode="decimal"
              className={fieldClass}
              value={draft.tax_other_income_eur ?? ""}
              placeholder="0"
              onChange={(e) => set({ tax_other_income_eur: num(e.target.value) ?? 0 })}
              aria-label="Other income this year"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-text-secondary">Health insurance</span>
            <select
              className={fieldClass}
              value={draft.tax_health_insurance ?? "unknown"}
              onChange={(e) => set({ tax_health_insurance: e.target.value as TaxSettings["tax_health_insurance"] })}
              aria-label="Health insurance"
            >
              <option value="unknown">Not set</option>
              <option value="de_family">German family insurance</option>
              <option value="de_own">Own German insurance</option>
              <option value="foreign">Co-insured abroad (e.g. Austrian ÖGK)</option>
            </select>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={draft.tax_bafoeg === true}
              onChange={(e) => set({ tax_bafoeg: e.target.checked })}
              aria-label="Receiving BAföG"
            />
            Receiving BAföG
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={draft.tax_spouse_allowance === true}
              onChange={(e) => set({ tax_spouse_allowance: e.target.checked })}
              aria-label="Married, filing jointly"
            />
            Married, filing jointly
          </label>
        </fieldset>

        <div className="flex items-center gap-3">
          <Button size="sm" onClick={() => save.mutate(draft)} disabled={over || badUntil || save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          <span className="text-xs text-text-muted">Estimate, not tax advice. Your banks' statements are the source of truth.</span>
        </div>
      </CardContent>
    </Card>
  );
}
