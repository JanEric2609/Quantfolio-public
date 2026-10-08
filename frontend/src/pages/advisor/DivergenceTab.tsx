import { formatPercent } from "../../lib/format";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { GitCompareArrows, Lock } from "lucide-react";
import { Card } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { EmptyState } from "../../components/composed/EmptyState";
import { cn } from "../../lib/utils";
import { api, type WhatWouldChangeResponse, type WhatWouldChangeRow } from "../../lib/api";

const pct = (v: number | null | undefined, digits = 1) =>
  formatPercent(v, { digits });

const ACTION_VARIANT: Record<WhatWouldChangeRow["action"], "success" | "info" | "warning" | "danger"> = {
  new: "success",
  add: "info",
  trim: "warning",
  exit: "danger",
};

export function DivergenceTab() {
  const query = useQuery({
    queryKey: ["advisor-what-would-change"],
    queryFn: () => api<WhatWouldChangeResponse>("/api/advisor/what-would-change"),
  });

  if (query.isLoading) {
    return <Card className="p-6 text-sm text-text-muted">Computing champion vs real diff…</Card>;
  }
  const data = query.data;
  if (!data || (data.rows.length === 0 && data.note)) {
    return (
      <EmptyState
        icon={GitCompareArrows}
        title="No champion book to compare yet"
        description={data?.note ?? "Run the advisor loop so the champion strategy builds a paper book to diff against your real holdings."}
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* Research-only banner: since Phase 4 graduation no longer makes this advice. */}
      <Card className="p-4 flex items-start gap-3 border-l-4 border-l-border">
        <Lock className="h-5 w-5 text-text-muted shrink-0 mt-0.5" />
        <div className="text-sm">
          <p className="font-medium">
            Research only{data.graduated ? " (the champion has graduated)" : ""}: nothing here is advice for your real book.
          </p>
          <p className="text-text-secondary text-xs mt-0.5">
            {data.research_only ??
              "The LLM loop is paper-only and has no evidence card, so it never sets weights for your real book."}{" "}
            What to do with your money each month is on{" "}
            <Link to="/plan" className="text-accent underline">This month</Link>. Nothing is ever executed at DKB.
          </p>
        </div>
      </Card>

      {data.rows.length === 0 ? (
        <Card className="p-6 text-sm text-text-muted">
          The champion's book currently matches your real holdings within 1% per position —
          nothing to change.
        </Card>
      ) : (
        <Card className="p-0 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-text-muted border-b border-border">
                <th className="py-2 pl-4 pr-3">Position</th>
                <th className="py-2 pr-3">Action</th>
                <th className="py-2 pr-3 text-right">Your weight</th>
                <th className="py-2 pr-3 text-right">Champion</th>
                <th className="py-2 pr-3 text-right">Δ</th>
                <th className="py-2 pr-3 text-right" title="The model's own confidence, not a probability. A calibrated probability appears once 100 issue dates have resolved.">Model's own confidence (not a probability) → calibrated</th>
                <th className="py-2 pr-3 text-right">MC P5/P50/P95</th>
                <th className="py-2 pr-4">Thesis</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.ticker} className="border-b border-border/50 align-top">
                  <td className="py-2 pl-4 pr-3">
                    <span className="font-medium">{row.ticker}</span>
                    {row.name && <span className="block text-xs text-text-muted">{row.name}</span>}
                  </td>
                  <td className="py-2 pr-3">
                    <Badge variant={ACTION_VARIANT[row.action]} className="uppercase text-[10px]">
                      {row.action}
                    </Badge>
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">{pct(row.real_weight)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{pct(row.champion_weight)}</td>
                  <td
                    className={cn(
                      "py-2 pr-3 text-right tabular-nums font-medium",
                      row.delta > 0 ? "text-success" : "text-danger",
                    )}
                  >
                    {row.delta > 0 ? "+" : ""}
                    {pct(row.delta)}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {row.confidence_raw === null ? (
                      <span className="text-text-muted">no prediction</span>
                    ) : (
                      <>
                        {pct(row.confidence_raw, 0)} →{" "}
                        <span className="font-medium">
                          {row.confidence_calibrated === null
                            ? "not yet calibrated"
                            : pct(row.confidence_calibrated, 0)}
                        </span>
                      </>
                    )}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums text-xs">
                    {row.mc
                      ? `${pct(row.mc.p5)} / ${pct(row.mc.p50)} / ${pct(row.mc.p95)}`
                      : "—"}
                  </td>
                  <td className="py-2 pr-4 text-xs text-text-secondary max-w-xs">
                    {row.thesis ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <p className="text-[11px] text-text-muted">
        Ranked by conviction × |weight delta|. Estimate · not financial advice. All trades are
        yours to make at DKB — nothing here executes anything.
      </p>
    </div>
  );
}
