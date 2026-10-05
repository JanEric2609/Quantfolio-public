import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { AlertTriangle, Calculator } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { formatCurrency, formatPercentPoints } from "../../lib/format";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Input } from "../../components/ui/input";
import { Label } from "../../components/ui/label";
import { MetricGroup } from "../../components/composed/MetricGroup";
import { KpiTile } from "../../components/composed/KpiTile";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type Box3Overview = {
  tax_year: number;
  jurisdiction: string;
  label: string;
  estimate: boolean;
  not_tax_advice: boolean;
  wealth: {
    total_eur: number;
    bank_deposits_eur: number;
    investments_eur: number;
    debts_eur: number;
    tax_free_allowance_eur: number;
    taxable_eur: number;
    is_prorated: boolean;
    days_resident_nl: number | null;
  };
  calculation: {
    bank_rate_pct: number;
    investment_rate_pct: number;
    debt_rate_pct: number;
    bank_fictitious_return_eur: number;
    investment_fictitious_return_eur: number;
    debt_deduction_eur: number;
    fictitious_return_rate_pct: number;
    fictitious_return_eur: number;
    box3_tax_eur: number;
    effective_tax_rate_pct: number;
  };
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function euroInput(val: string): number {
  const n = parseFloat(val.replace(",", "."));
  return isNaN(n) || n < 0 ? 0 : n;
}

// The NL Box 3 API reports rates in percent units (5.88 = 5,88 %).
function fmtPct(v: number, d = 2): string {
  return formatPercentPoints(v, { digits: d });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Box3Tab() {
  const { year } = useOutletContext<{ year: number }>();

  // Manual override inputs (optional — user can enter their own wealth figures)
  const [bankDeposits, setBankDeposits] = useState<string>("");
  const [investments, setInvestments] = useState<string>("");
  const [debts, setDebts] = useState<string>("");
  const [manualOverride, setManualOverride] = useState(false);
  const [overrideParams, setOverrideParams] = useState<{
    bank_deposits: number;
    investments: number;
    debts: number;
  } | null>(null);

  // Build the API URL — use override params when the user has submitted them
  const queryParams = new URLSearchParams({ year: String(year) });
  if (overrideParams) {
    queryParams.set("bank_deposits", String(overrideParams.bank_deposits));
    queryParams.set("investments", String(overrideParams.investments));
    queryParams.set("debts", String(overrideParams.debts));
  }

  const box3 = useQuery({
    queryKey: ["tax", "box3", year, overrideParams],
    queryFn: () => api<Box3Overview>(`/api/tax/box3?${queryParams.toString()}`),
    staleTime: 60_000,
  });

  const handleApplyOverride = () => {
    setOverrideParams({
      bank_deposits: euroInput(bankDeposits),
      investments: euroInput(investments),
      debts: euroInput(debts),
    });
  };

  const handleClearOverride = () => {
    setOverrideParams(null);
    setBankDeposits("");
    setInvestments("");
    setDebts("");
  };

  // A DE resident (or disabled estimation) gets {disabled, reason} and no
  // wealth block; reading result.wealth would crash the tab.
  const disabled = box3.data as { disabled?: boolean; reason?: string } | undefined;
  const disabledReason = disabled?.disabled ? disabled.reason ?? "Box 3 estimation is not available." : null;
  const result = disabledReason ? undefined : box3.data;

  return (
    <div className="space-y-6">
      {/* Disclaimer */}
      <div className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm text-warn">
        <AlertTriangle size={14} className="mt-0.5 shrink-0" />
        <span>
          Estimate only. Official Belastingdienst aanslag remains the source of truth.
          Not tax advice — always consult a certified Dutch tax adviser (belastingadviseur).
        </span>
        <Badge variant="secondary" className="ml-auto shrink-0 text-xs">estimate</Badge>
        <Badge variant="secondary" className="shrink-0 text-xs">not tax advice</Badge>
      </div>

      {/* Optional manual wealth input */}
      <div className="rounded-lg border border-border bg-surface p-4 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-text-primary">
            Manual wealth override
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="text-xs"
            onClick={() => setManualOverride((v) => !v)}
          >
            {manualOverride ? "Hide" : "Enter amounts manually"}
          </Button>
        </div>
        {!manualOverride && (
          <p className="text-xs text-text-secondary">
            By default, wealth is derived from your tracked portfolio holdings.
            Use manual override to model a specific scenario.
          </p>
        )}
        {manualOverride && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="box3-bank">Bank / Savings deposits (€)</Label>
                <Input
                  id="box3-bank"
                  type="number"
                  min="0"
                  step="100"
                  placeholder="0"
                  value={bankDeposits}
                  onChange={(e) => setBankDeposits(e.target.value)}
                />
                <p className="text-xs text-text-muted">Bucket 1 — savings rate ~1.44%</p>
              </div>
              <div className="space-y-1">
                <Label htmlFor="box3-investments">Investments — beleggingen (€)</Label>
                <Input
                  id="box3-investments"
                  type="number"
                  min="0"
                  step="100"
                  placeholder="0"
                  value={investments}
                  onChange={(e) => setInvestments(e.target.value)}
                />
                <p className="text-xs text-text-muted">Bucket 2 — investment rate ~5.88%</p>
              </div>
              <div className="space-y-1">
                <Label htmlFor="box3-debts">Debts / Liabilities (€)</Label>
                <Input
                  id="box3-debts"
                  type="number"
                  min="0"
                  step="100"
                  placeholder="0"
                  value={debts}
                  onChange={(e) => setDebts(e.target.value)}
                />
                <p className="text-xs text-text-muted">Bucket 3 — deductible at ~2.62%</p>
              </div>
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                onClick={handleApplyOverride}
                className="flex items-center gap-2"
              >
                <Calculator size={14} />
                Apply &amp; recalculate
              </Button>
              {overrideParams && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={handleClearOverride}
                >
                  Clear override
                </Button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Loading / error states */}
      {box3.isLoading && (
        <div className="text-sm text-text-secondary">Computing Box 3…</div>
      )}
      {box3.isError && (
        <div className="rounded border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          {box3.error instanceof Error ? box3.error.message : "Failed to load Box 3 data."}
        </div>
      )}

      {disabledReason && (
        <div className="rounded-md border border-border bg-surface-2 p-3 text-sm text-text-secondary">{disabledReason}</div>
      )}

      {/* Results */}
      {result && (
        <div className="space-y-4">
          {/* Summary KPIs */}
          <MetricGroup title={`Box 3 wealth overview — ${result.tax_year}`}>
            <KpiTile
              label="Total wealth"
              value={formatCurrency(result.wealth.total_eur, "EUR")}
            />
            <KpiTile
              label={
                result.wealth.is_prorated && result.wealth.days_resident_nl != null
                  ? `Heffingvrij vermogen (${result.wealth.days_resident_nl}d)`
                  : "Heffingvrij vermogen"
              }
              value={formatCurrency(result.wealth.tax_free_allowance_eur, "EUR")}
            />
            <KpiTile
              label="Taxable wealth"
              value={formatCurrency(result.wealth.taxable_eur, "EUR")}
            />
            <KpiTile
              label="Box 3 tax owed"
              value={formatCurrency(result.calculation.box3_tax_eur, "EUR")}
            />
          </MetricGroup>

          {/* Three-bucket breakdown table */}
          <Card className="border-border bg-surface">
            <CardHeader>
              <CardTitle>
                Three-bucket fictitious return (forfaitair rendement)
              </CardTitle>
            </CardHeader>
            <CardContent className="overflow-x-auto p-0">
              <table className="w-full text-sm">
                <thead className="border-b border-border bg-surface-2 text-left text-text-secondary">
                  <tr>
                    <th className="px-4 py-2 font-medium">Bucket</th>
                    <th className="px-4 py-2 font-medium text-right">Value</th>
                    <th className="px-4 py-2 font-medium text-right">Rate</th>
                    <th className="px-4 py-2 font-medium text-right">
                      Fictitious return
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr className="border-b border-border">
                    <td className="px-4 py-3 text-text-primary">
                      Bank deposits / savings
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-primary">
                      {formatCurrency(result.wealth.bank_deposits_eur, "EUR")}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-secondary">
                      {fmtPct(result.calculation.bank_rate_pct)}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-primary">
                      {formatCurrency(result.calculation.bank_fictitious_return_eur, "EUR")}
                    </td>
                  </tr>
                  <tr className="border-b border-border">
                    <td className="px-4 py-3 text-text-primary">
                      Investments (beleggingen)
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-primary">
                      {formatCurrency(result.wealth.investments_eur, "EUR")}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-secondary">
                      {fmtPct(result.calculation.investment_rate_pct)}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-primary">
                      {formatCurrency(result.calculation.investment_fictitious_return_eur, "EUR")}
                    </td>
                  </tr>
                  <tr className="border-b border-border last:border-b-0">
                    <td className="px-4 py-3 text-text-primary">
                      Debts (schulden) — deductible
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-primary">
                      {formatCurrency(result.wealth.debts_eur, "EUR")}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-secondary">
                      {fmtPct(result.calculation.debt_rate_pct)}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-primary">
                      −{formatCurrency(result.calculation.debt_deduction_eur, "EUR")}
                    </td>
                  </tr>
                </tbody>
                <tfoot className="border-t-2 border-border bg-surface-2">
                  <tr>
                    <td
                      className="px-4 py-3 font-medium text-text-primary"
                      colSpan={3}
                    >
                      Net fictitious return
                    </td>
                    <td className="px-4 py-3 text-right font-semibold tabular-nums text-text-primary">
                      {formatCurrency(result.calculation.fictitious_return_eur, "EUR")}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </CardContent>
          </Card>

          {/* Tax derivation */}
          <Card className="border-border bg-surface">
            <CardHeader>
              <CardTitle>Tax derivation</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="divide-y divide-border text-sm">
                <div className="flex justify-between py-2">
                  <dt className="text-text-secondary">
                    Gross assets (bank + investments)
                  </dt>
                  <dd className="tabular-nums text-text-primary">
                    {formatCurrency(
                      result.wealth.bank_deposits_eur + result.wealth.investments_eur,
                      "EUR",
                    )}
                  </dd>
                </div>
                <div className="flex justify-between py-2">
                  <dt className="text-text-secondary">Debts</dt>
                  <dd className="tabular-nums text-text-primary">
                    −{formatCurrency(result.wealth.debts_eur, "EUR")}
                  </dd>
                </div>
                <div className="flex items-center justify-between py-2">
                  <dt className="flex items-center gap-2 text-text-secondary">
                    Heffingvrij vermogen
                    {result.wealth.is_prorated && (
                      <Badge variant="secondary" className="text-xs">
                        prorated
                      </Badge>
                    )}
                  </dt>
                  <dd className="tabular-nums text-text-primary">
                    −{formatCurrency(result.wealth.tax_free_allowance_eur, "EUR")}
                  </dd>
                </div>
                <div className="flex justify-between py-2 font-medium">
                  <dt className="text-text-primary">Taxable wealth</dt>
                  <dd className="tabular-nums text-text-primary">
                    {formatCurrency(result.wealth.taxable_eur, "EUR")}
                  </dd>
                </div>
                <div className="flex justify-between py-2">
                  <dt className="text-text-secondary">
                    Net fictitious return (blended{" "}
                    {fmtPct(result.calculation.fictitious_return_rate_pct, 4)})
                  </dt>
                  <dd className="tabular-nums text-text-primary">
                    {formatCurrency(result.calculation.fictitious_return_eur, "EUR")}
                  </dd>
                </div>
                <div className="flex justify-between border-t-2 border-border py-2 font-semibold">
                  <dt className="text-text-primary">Box 3 tax</dt>
                  <dd className="tabular-nums text-danger">
                    {formatCurrency(result.calculation.box3_tax_eur, "EUR")}
                  </dd>
                </div>
                <div className="flex justify-between py-2 text-xs">
                  <dt className="text-text-muted">Effective tax rate on total wealth</dt>
                  <dd className="tabular-nums text-text-secondary">
                    {fmtPct(result.calculation.effective_tax_rate_pct)}
                  </dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          <p className="text-xs text-text-muted">
            Rates sourced from Belastingdienst published rates for {result.tax_year}.
            Final assessment may differ. Always consult a qualified tax adviser.
          </p>
        </div>
      )}
    </div>
  );
}
