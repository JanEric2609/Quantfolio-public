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
import { formatCurrency, formatPercent } from "../../lib/format";
import { DiffTable } from "./DiffTable";
import {
  api,
  type MandateDivergence,
  type MandateAdviceCardUpdateResponse,
} from "../../lib/api";
import { MANDATES, portfolioOf, type MandateLetter, type MandatesOutletContext } from "./mandateContext";

export function DivergenceTab() {
  const { portfolios, portfoliosLoading } = useOutletContext<MandatesOutletContext>();
  if (portfoliosLoading) return <Card className="p-6 text-sm text-text-muted">Loading mandates…</Card>;
  return (
    <div className="space-y-8">
      {MANDATES.map((m) => (
        <section key={m} className="space-y-3" aria-label={`Mandate ${m} divergence`}>
          <h2 className="text-sm font-semibold text-text-primary">Mandate {m}</h2>
          <MandateDivergence mandate={m} portfolioId={portfolioOf(portfolios, m)?.id ?? null} />
        </section>
      ))}
      <p className="text-[11px] text-text-muted">
        Estimate · not financial advice. Nothing here is ever executed at a broker — trades are always yours to make.
      </p>
    </div>
  );
}

function MandateDivergence({ mandate, portfolioId }: { mandate: MandateLetter; portfolioId: string | null }) {
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

  const { missing_in_real, missing_in_paper, weight_diffs, summary } = data.paper_vs_real;
  const pendingCards = data.advice_cards.filter((c) => c.status === "new");
  const hasNothing = missing_in_real.length === 0 && missing_in_paper.length === 0 && weight_diffs.length === 0;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="divergence-summary">
        <KpiTile
          label="Overlap with your book"
          value={summary?.overlap_pct == null ? "–" : `${summary.overlap_pct.toLocaleString("de-DE", { maximumFractionDigits: 1 })} %`}
        />
        <KpiTile label="Only in the mandate" value={summary?.only_in_mandate ?? missing_in_real.length} mono />
        <KpiTile label="Only in your book" value={summary?.only_in_book ?? missing_in_paper.length} mono />
        <KpiTile label="Different quantity" value={summary?.quantity_differs ?? weight_diffs.filter((w) => w.quantity_diff !== 0).length} mono />
      </div>
      <p className="text-xs text-text-muted">
        Performance delta {formatPercent(data.performance_delta)}
        {summary && summary.largest_gaps.length > 0 && (
          <>
            {" · largest gaps: "}
            {summary.largest_gaps.map((g) => `${g.ticker ?? "?"} ${formatCurrency(g.gap_eur, "EUR", { digits: 0 })}`).join(", ")}
          </>
        )}
      </p>

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
        <DiffTable onlyInMandate={missing_in_real} onlyInBook={missing_in_paper} quantityDiffs={weight_diffs} />
      )}
    </div>
  );
}
