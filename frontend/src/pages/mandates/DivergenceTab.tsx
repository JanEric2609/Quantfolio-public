import { useOutletContext } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { GitCompareArrows, Check, X } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { EmptyState } from "../../components/composed/EmptyState";
import { KpiTile } from "../../components/composed/KpiTile";
import { formatPercent } from "../../lib/format";
import {
  api,
  type MandateDivergence,
  type MandateAdviceCardUpdateResponse,
} from "../../lib/api";
import type { MandatesOutletContext } from "./MandatesHub";

function HoldingsDiffTable({
  title,
  rows,
}: {
  title: string;
  rows: { isin: string; ticker: string | null; name?: string | null; quantity: string }[];
}) {
  if (rows.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">{title}</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-text-muted border-b border-border">
              <th className="py-2 pl-4 pr-3">Ticker</th>
              <th className="py-2 pr-3">Name</th>
              <th className="py-2 pr-4 text-right">Quantity</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.isin} className="border-b border-border/50 last:border-0">
                <td className="py-2 pl-4 pr-3 font-mono">{r.ticker ?? "—"}</td>
                <td className="py-2 pr-3 text-text-secondary">{r.name ?? "—"}</td>
                <td className="py-2 pr-4 text-right tabular-nums">{r.quantity}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

export function DivergenceTab() {
  const { portfolioId, mandate } = useOutletContext<MandatesOutletContext>();
  const queryClient = useQueryClient();

  const divergenceQuery = useQuery({
    queryKey: ["llm-portfolio-divergence", portfolioId],
    queryFn: () => api<MandateDivergence>(`/api/llm-portfolio/${portfolioId}/divergence`),
    enabled: portfolioId != null,
  });

  const updateCard = useMutation({
    mutationFn: ({ cardId, status }: { cardId: string; status: "acknowledged" | "dismissed" }) =>
      api<MandateAdviceCardUpdateResponse>(`/api/llm-portfolio/advice-cards/${cardId}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["llm-portfolio-divergence", portfolioId] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to update advice card"),
  });

  if (portfolioId == null) {
    return (
      <EmptyState
        icon={GitCompareArrows}
        title={`No Mandate ${mandate} portfolio yet`}
        description="Create the mandate on the Overview tab before divergence can be computed."
      />
    );
  }

  if (divergenceQuery.isLoading) {
    return <Card className="p-6 text-sm text-text-muted">Computing paper vs real diff…</Card>;
  }

  const data = divergenceQuery.data;
  if (!data) {
    return <Card className="p-6 text-sm text-text-muted">Divergence data unavailable.</Card>;
  }

  const { missing_in_real, missing_in_paper, weight_diffs } = data.paper_vs_real;
  const pendingCards = data.advice_cards.filter((c) => c.status === "new");
  const hasNothing = missing_in_real.length === 0 && missing_in_paper.length === 0 && weight_diffs.length === 0;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <KpiTile
          label="Performance Delta"
          value={formatPercent(data.performance_delta)}
          tone={Math.abs(data.performance_delta) > 0.05 ? "bad" : "neutral"}
        />
        <KpiTile label="Positions only in Mandate" value={missing_in_real.length} mono />
        <KpiTile label="Positions only in Real book" value={missing_in_paper.length} mono />
      </div>

      {pendingCards.length > 0 && (
        <div className="space-y-2">
          {pendingCards.map((card) => (
            <Card key={card.id} className="p-4 flex items-start justify-between gap-4 border-l-4 border-l-warn">
              <div className="text-sm">
                <p>{card.advice_text}</p>
                <p className="text-xs text-text-muted mt-1">
                  Delta {formatPercent(card.performance_delta)}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={updateCard.isPending}
                  onClick={() => updateCard.mutate({ cardId: card.id, status: "acknowledged" })}
                >
                  <Check className="h-4 w-4 mr-1" /> Acknowledge
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={updateCard.isPending}
                  onClick={() => updateCard.mutate({ cardId: card.id, status: "dismissed" })}
                >
                  <X className="h-4 w-4 mr-1" /> Dismiss
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {hasNothing ? (
        <Card className="p-6 text-sm text-text-muted">
          No ISIN-matched divergence between the mandate's paper holdings and your real book right now.
        </Card>
      ) : (
        <div className="space-y-4">
          <HoldingsDiffTable title="Only in the mandate (not in your real book)" rows={missing_in_real} />
          <HoldingsDiffTable title="Only in your real book (not in the mandate)" rows={missing_in_paper} />
          {weight_diffs.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">Quantity differences</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-text-muted border-b border-border">
                      <th className="py-2 pl-4 pr-3">Ticker</th>
                      <th className="py-2 pr-3 text-right">Mandate qty</th>
                      <th className="py-2 pr-3 text-right">Real qty</th>
                      <th className="py-2 pr-4 text-right">Diff</th>
                    </tr>
                  </thead>
                  <tbody>
                    {weight_diffs.map((w) => (
                      <tr key={w.isin} className="border-b border-border/50 last:border-0">
                        <td className="py-2 pl-4 pr-3 font-mono">{w.ticker ?? "—"}</td>
                        <td className="py-2 pr-3 text-right tabular-nums">{w.paper_quantity}</td>
                        <td className="py-2 pr-3 text-right tabular-nums">{w.real_quantity}</td>
                        <td
                          className={`py-2 pr-4 text-right tabular-nums font-medium ${w.quantity_diff > 0 ? "text-success" : w.quantity_diff < 0 ? "text-danger" : ""}`}
                        >
                          {w.quantity_diff > 0 ? "+" : ""}
                          {w.quantity_diff}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )}
        </div>
      )}

      <p className="text-[11px] text-text-muted">
        Estimate · not financial advice. Nothing here is ever executed at DKB — trades are always
        yours to make.
      </p>
    </div>
  );
}
