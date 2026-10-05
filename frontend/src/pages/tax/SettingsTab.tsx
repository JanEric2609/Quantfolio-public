import { Link } from "react-router-dom";
import { AlertTriangle, Settings2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { formatPercent } from "../../lib/format";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { useSettingsModel, useSettingsValues } from "../settings/lib/schema";
import { formatValue } from "../settings/lib/values";
import { TaxStatusCard } from "./components/TaxStatusCard";

type BasiszinsRate = {
  year: number;
  basiszins: number;
};

export function SettingsTab() {
  const basiszins = useQuery({
    queryKey: ["tax", "basiszins"],
    queryFn: () => api<{ items: BasiszinsRate[] }>("/api/tax/basiszins"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 rounded-md border border-info/40 bg-info/10 p-3 text-sm text-info">
        <AlertTriangle size={14} />
        Estimate. Not tax advice. Broker statements are the source of truth.
      </div>
      <TaxStatusCard />
      <TaxInputsSummary />
      <Card className="border-border bg-surface">
        <CardHeader>
          <CardTitle>Basiszins by year</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead className="border-b border-border bg-surface-2 text-left text-text-secondary">
                <tr>
                  <th className="px-4 py-2 font-medium">Tax year</th>
                  <th className="px-4 py-2 font-medium">Basiszins</th>
                </tr>
              </thead>
              <tbody>
                {basiszins.data?.items.map((rate) => (
                  <tr key={rate.year} className="border-b border-border last:border-b-0">
                    <td className="px-4 py-2">{rate.year}</td>
                    <td className="px-4 py-2">{formatPercent(rate.basiszins, { digits: 2 })}</td>
                  </tr>
                ))}
                {!basiszins.isPending && !basiszins.data?.items.length && (
                  <tr>
                    <td colSpan={2} className="px-4 py-6 text-center text-text-muted">
                      No Basiszins rates available.
                    </td>
                  </tr>
                )}
                {basiszins.isPending && (
                  <tr>
                    <td colSpan={2} className="px-4 py-6 text-center text-text-muted">
                      Loading Basiszins rates...
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

/** Read-only view of every tax input; the rarer ones are edited in Control Center › Tax. */
function TaxInputsSummary() {
  const model = useSettingsModel();
  const values = useSettingsValues();
  const entries = model?.entriesForGroup("tax") ?? [];

  return (
    <Card className="border-border bg-surface">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle>All tax inputs</CardTitle>
        <Button asChild variant="outline" size="sm">
          <Link to="/settings/tax">
            <Settings2 className="mr-2 h-4 w-4" aria-hidden="true" /> Edit in Control Center
          </Link>
        </Button>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-1 gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
          {entries.map((entry) => (
            <div key={entry.key} className="flex justify-between gap-4 border-b border-border/60 py-1.5">
              <dt className="text-text-secondary">{entry.label}</dt>
              <dd className="text-right font-medium tabular-nums">
                {formatValue(entry, values.data?.settings?.[entry.key] ?? entry.default)}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}
