import { useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { formatCurrency, formatNumber } from "../../lib/format";
import { getLearningSleeve, setLearningFillPrice, type LearningSleeve } from "../../lib/api";
import { Table } from "../ui/table";

/** The learning sleeve: what you bought, the same amounts in the world ETF, and the fills against paper prices. */
export function LearningSleeveCard() {
  const [data, setData] = useState<LearningSleeve | null>(null);
  const [prices, setPrices] = useState<Record<string, string>>({});
  useEffect(() => {
    getLearningSleeve().then(setData).catch(() => setData(null));
  }, []);
  if (!data || data.count_planned === 0) return null;
  const save = (id: string) => {
    const v = Number(prices[id]?.replace(",", "."));
    if (Number.isFinite(v) && v > 0) setLearningFillPrice(id, v).then(setData).catch(() => undefined);
  };
  return (
    <Card data-testid="learning-sleeve">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Learning sleeve</CardTitle>
        <CardDescription>{data.note}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p>
          {data.count_filled} of {data.count_planned} planned purchases made. Put in {formatCurrency(data.invested_eur)};
          worth {formatCurrency(data.real_value_eur)} now. The same amounts in the world ETF would be worth{" "}
          {formatCurrency(data.shadow_value_eur)}.
        </p>
        <div className="overflow-x-auto">
          <Table className="w-full text-left text-xs">
            <thead className="text-text-muted">
              <tr><th className="py-1 pr-3">Month</th><th className="pr-3">Pick</th><th className="pr-3">Amount</th>
                <th className="pr-3">Status</th><th className="pr-3">Fill vs paper</th><th>Real fill price</th></tr>
            </thead>
            <tbody>
              {data.purchases.map((p) => (
                <tr key={p.id} className="border-t border-border">
                  <td className="py-1 pr-3">{p.month.slice(0, 7)}</td>
                  <td className="pr-3">{p.ticker}</td>
                  <td className="pr-3">{formatCurrency(p.amount_eur)}</td>
                  <td className="pr-3">{p.status}</td>
                  <td className="pr-3">{p.fill_vs_paper_pct == null ? "—" : `${formatNumber(p.fill_vs_paper_pct, { digits: 2 })} %`}</td>
                  <td>
                    {p.status === "filled" ? (
                      <span className="flex items-center gap-1">
                        <input aria-label={`Real fill price for ${p.ticker}`} inputMode="decimal"
                          className="w-20 rounded border border-border bg-transparent px-1"
                          placeholder={p.fill_price_eur == null ? "" : `${formatNumber(p.fill_price_eur, { digits: 2 })}${p.fill_price_is_implied ? " (implied)" : ""}`}
                          value={prices[p.id] ?? ""} onChange={(e) => setPrices({ ...prices, [p.id]: e.target.value })} />
                        <button type="button" className="underline" onClick={() => save(p.id)}>Save</button>
                      </span>
                    ) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
