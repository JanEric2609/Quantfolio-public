import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Skeleton } from "../ui/skeleton";
import { getIngredientAttribution } from "../../lib/api";
import { formatNumber } from "../../lib/format";

function cell(value: number | null, digits = 2): string {
  return value == null ? "—" : formatNumber(value, { digits });
}

export function IngredientAttributionCard() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["ingredient-attribution"],
    queryFn: getIngredientAttribution,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Ranking ingredients on live data</CardTitle>
        <CardDescription>
          Per-ingredient rank IC from the shadow ledger against resolved forward excess returns
          (21 trading days). Information only — it feeds no score and changes no gate. Correlated
          stocks count as fewer independent voices than the names suggest.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : error ? (
          <p className="text-sm text-danger">Failed to load ingredient attribution.</p>
        ) : !data || data.n_runs === 0 ? (
          <p className="text-sm text-text-secondary">
            No resolved ranking cohorts yet — this table fills in once Discover runs resolve.
          </p>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-text-secondary">
              {data.n_runs} runs · {data.n_stocks} stocks
              {data.effective_n_stocks != null && (
                <> · {formatNumber(data.effective_n_stocks, { digits: 0 })} effective independent voices</>
              )}
              {data.rho_bar != null && <> · mean pairwise correlation {formatNumber(data.rho_bar, { digits: 2 })}</>}
              .
            </p>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-text-secondary">
                  <th className="py-1 pr-3 font-medium">Ingredient</th>
                  <th className="py-1 pr-3 text-right font-medium">Runs</th>
                  <th className="py-1 pr-3 text-right font-medium">Mean IC</th>
                  <th className="py-1 pr-3 text-right font-medium">NW t</th>
                  <th className="py-1 text-right font-medium">p (Holm)</th>
                </tr>
              </thead>
              <tbody>
                {data.ingredients.map((row) => (
                  <tr key={row.signal} className="border-b border-border align-top last:border-0">
                    <td className="py-2 pr-3 font-medium text-text-primary">{row.signal}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{row.n_runs}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{cell(row.mean_ic, 3)}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{cell(row.t_stat)}</td>
                    <td className="py-2 text-right tabular-nums">{cell(row.p_holm, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
