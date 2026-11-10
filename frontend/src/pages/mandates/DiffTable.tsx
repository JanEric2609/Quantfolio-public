import { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";
import { Card, CardContent } from "../../components/ui/card";
import { formatCurrency, formatNumber } from "../../lib/format";
import type { MandateMissingHolding, MandateWeightDiff } from "../../lib/api";

type Kind = "only_in_mandate" | "only_in_book" | "quantity_differs";
type Filter = "all" | Kind;

const KIND_LABEL: Record<Kind, string> = {
  only_in_mandate: "Only in mandate",
  only_in_book: "Only in your book",
  quantity_differs: "Quantity differs",
};

export interface DiffRow {
  key: string;
  ticker: string | null;
  name: string | null;
  kind: Kind;
  mandateQty: string | null;
  bookQty: string | null;
  gapEur: number;
}

/** One list of differences, biggest gap in euros first. */
export function buildRows(
  onlyInMandate: MandateMissingHolding[],
  onlyInBook: MandateMissingHolding[],
  quantityDiffs: MandateWeightDiff[],
): DiffRow[] {
  const rows: DiffRow[] = [
    ...onlyInMandate.map((r) => ({
      key: `m-${r.isin ?? r.ticker}`, ticker: r.ticker, name: r.name, kind: "only_in_mandate" as const,
      mandateQty: r.quantity, bookQty: null, gapEur: r.value_eur ?? 0,
    })),
    ...onlyInBook.map((r) => ({
      key: `b-${r.isin ?? r.ticker}`, ticker: r.ticker, name: r.name, kind: "only_in_book" as const,
      mandateQty: null, bookQty: r.quantity, gapEur: r.value_eur ?? 0,
    })),
    ...quantityDiffs
      .filter((w) => w.quantity_diff !== 0)
      .map((w) => ({
        key: `q-${w.isin ?? w.ticker}`, ticker: w.ticker, name: null, kind: "quantity_differs" as const,
        mandateQty: w.paper_quantity, bookQty: w.real_quantity,
        gapEur: Math.abs((w.paper_value_eur ?? 0) - (w.real_value_eur ?? 0)),
      })),
  ];
  return rows.sort((a, b) => b.gapEur - a.gapEur);
}

export function DiffTable({
  onlyInMandate, onlyInBook, quantityDiffs,
}: {
  onlyInMandate: MandateMissingHolding[];
  onlyInBook: MandateMissingHolding[];
  quantityDiffs: MandateWeightDiff[];
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const rows = useMemo(() => buildRows(onlyInMandate, onlyInBook, quantityDiffs), [onlyInMandate, onlyInBook, quantityDiffs]);
  const shown = filter === "all" ? rows : rows.filter((r) => r.kind === filter);
  return (
    <details className="group rounded-md border border-border bg-surface" data-testid="diff-details">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium text-text-primary">
        <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" />
        Differences ({rows.length})
      </summary>
      <Card className="border-0 shadow-none">
        <CardContent className="p-0">
          <div className="flex flex-wrap gap-2 px-4 pb-3" role="group" aria-label="Filter differences">
            {(["all", "only_in_mandate", "only_in_book", "quantity_differs"] as Filter[]).map((f) => (
              <button
                key={f}
                type="button"
                aria-pressed={filter === f}
                onClick={() => setFilter(f)}
                className={`rounded-full border px-3 py-1 text-xs ${filter === f ? "border-accent text-accent" : "border-border text-text-muted"}`}
              >
                {f === "all" ? "All" : KIND_LABEL[f]}
              </button>
            ))}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-text-muted">
                  <th className="py-2 pl-4 pr-3">Ticker</th>
                  <th className="py-2 pr-3">Difference</th>
                  <th className="py-2 pr-3 text-right">Mandate qty</th>
                  <th className="py-2 pr-3 text-right">Book qty</th>
                  <th className="py-2 pr-4 text-right">Gap</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.key} className="border-b border-border/50 last:border-0" data-testid="diff-row">
                    <td className="py-2 pl-4 pr-3 font-mono">{r.ticker ?? "—"}</td>
                    <td className="py-2 pr-3 text-text-secondary">{KIND_LABEL[r.kind]}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{r.mandateQty == null ? "—" : formatNumber(Number(r.mandateQty), { digits: 2, minDigits: 0 })}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{r.bookQty == null ? "—" : formatNumber(Number(r.bookQty), { digits: 2, minDigits: 0 })}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{formatCurrency(r.gapEur, "EUR", { digits: 0 })}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </details>
  );
}
