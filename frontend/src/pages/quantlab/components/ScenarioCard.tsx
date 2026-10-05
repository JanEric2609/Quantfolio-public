import { Trash2 } from "lucide-react";
import type { QuantSavedScenario } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { formatCurrency } from "../../../lib/format";

// The book and every saved scenario are in euro.
const money = (value: number) => formatCurrency(value, "EUR", { digits: 0 });

export function ScenarioCard({ scenario, selected, onSelect, onDelete }: { scenario: QuantSavedScenario; selected: boolean; onSelect: () => void; onDelete: () => void }) {
  return <article className="rounded-md border border-line bg-panel p-3">
    <div className="flex items-start gap-2">
      <label className="flex min-w-0 flex-1 gap-2"><input type="checkbox" checked={selected} onChange={onSelect} /><span className="truncate font-semibold text-text-primary">{scenario.name}</span></label>
      <Button size="icon" variant="ghost" title="Delete scenario" aria-label={`Delete scenario ${scenario.name}`} onClick={onDelete}><Trash2 className="h-4 w-4" /></Button>
    </div>
    <div className="mt-2 grid grid-cols-3 gap-2 text-xs"><span>P5 {money(scenario.p05_terminal)}</span><span>Median {money(scenario.median_terminal)}</span><span>P95 {money(scenario.p95_terminal)}</span></div>
  </article>;
}
