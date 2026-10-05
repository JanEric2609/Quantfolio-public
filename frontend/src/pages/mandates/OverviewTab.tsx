import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PlusCircle, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { KpiTile } from "../../components/composed/KpiTile";
import { PaperRunCard } from "../portfolio/components/PaperRunCard";
import { formatCurrency, formatNumber } from "../../lib/format";
import {
  api,
  type MandateListItem,
  type MandateCreateResponse,
  type MandateReviewTriggerResponse,
  type MandateReviewJobStatus,
} from "../../lib/api";

const MANDATES = ["A", "B"] as const;
const POLL_INTERVAL_MS = 2500;

function MandateCard({ portfolio, mandate }: { portfolio: MandateListItem | null; mandate: "A" | "B" }) {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);

  const createMandate = useMutation({
    mutationFn: () => api<MandateCreateResponse>("/api/llm-portfolio/create-mandate", {
      method: "POST",
      body: JSON.stringify({ mandate }),
    }),
    onSuccess: () => {
      toast.success(`Mandate ${mandate} portfolio created`);
      queryClient.invalidateQueries({ queryKey: ["llm-portfolio-list"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to create mandate"),
  });

  const triggerReview = useMutation({
    mutationFn: () =>
      api<MandateReviewTriggerResponse>(`/api/llm-portfolio/${portfolio!.id}/review`, { method: "POST" }),
    onSuccess: (res) => {
      setJobId(res.job_id);
      toast.message(`Review queued for Mandate ${mandate}`);
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to trigger review"),
  });

  const jobStatus = useQuery({
    queryKey: ["llm-portfolio-review-job", jobId],
    queryFn: () => api<MandateReviewJobStatus>(`/api/llm-portfolio/review/${jobId}`),
    enabled: jobId != null,
    refetchInterval: (query) => (query.state.data?.status === "scheduled" ? POLL_INTERVAL_MS : false),
  });

  const isRunning = jobId != null && jobStatus.data?.status !== "not_found_or_completed";

  // The review-job endpoint carries no result payload (see MandateReviewJobStatus) —
  // once polling shows the job is done, the only way to see what happened is
  // to refetch the list (updated total_value/cash_balance) and this mandate's
  // journal (the new decision row). Runs once per completed job (job_id dep).
  useEffect(() => {
    if (jobId != null && jobStatus.data?.status === "not_found_or_completed") {
      queryClient.invalidateQueries({ queryKey: ["llm-portfolio-list"] });
      queryClient.invalidateQueries({ queryKey: ["llm-portfolio-journal", portfolio?.id] });
      toast.success(`Mandate ${mandate} review finished — see the Journal tab.`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, jobStatus.data?.status]);

  if (!portfolio) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Mandate {mandate}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-text-muted">
            No paper portfolio for this mandate yet. Create one to start running weekly LLM reviews.
          </p>
          <Button size="sm" variant="outline" onClick={() => createMandate.mutate()} disabled={createMandate.isPending}>
            <PlusCircle className="h-4 w-4 mr-2" />
            {createMandate.isPending ? "Creating…" : "Create mandate"}
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base flex items-center gap-2">
          Mandate {mandate}
          <Badge variant="secondary" className="text-[10px]">{portfolio.managed_by}</Badge>
        </CardTitle>
        <Button
          size="sm"
          variant="outline"
          onClick={() => triggerReview.mutate()}
          disabled={triggerReview.isPending || isRunning}
        >
          <RefreshCw className={`h-4 w-4 mr-2 ${isRunning ? "animate-spin" : ""}`} />
          {isRunning ? "Reviewing…" : "Trigger review"}
        </Button>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-3 gap-3">
          <KpiTile label="Total Value" value={formatCurrency(Number(portfolio.total_value))} />
          <KpiTile label="Cash Balance" value={formatCurrency(Number(portfolio.cash_balance))} />
          <KpiTile label="Holdings" value={formatNumber(portfolio.holdings_count, { decimals: 0 })} />
        </div>
        <div className="mt-3">
          <PaperRunCard portfolioId={portfolio.id} invalidate={[["llm-portfolio-list"]]} />
        </div>
        {jobId != null && jobStatus.data?.status === "not_found_or_completed" && (
          <p className="mt-3 text-xs text-text-muted">
            Review finished — check the Journal tab for the decision.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function OverviewTab() {
  const portfoliosQuery = useQuery({
    queryKey: ["llm-portfolio-list"],
    queryFn: () => api<MandateListItem[]>("/api/llm-portfolio/"),
  });

  if (portfoliosQuery.isLoading) {
    return (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {[...Array(2)].map((_, i) => <Skeleton key={i} className="h-[220px] w-full" />)}
      </div>
    );
  }

  if (portfoliosQuery.isError) {
    return (
      <Card>
        <CardContent className="flex items-center justify-between gap-3 pt-6">
          <div className="text-sm text-danger">Failed to load mandate portfolios.</div>
          <Button variant="outline" size="sm" onClick={() => portfoliosQuery.refetch()}>Retry</Button>
        </CardContent>
      </Card>
    );
  }

  const byMandate = new Map((portfoliosQuery.data ?? []).map((p) => [p.mandate, p]));

  return (
    <div className="space-y-4">
      <p className="text-sm text-text-secondary max-w-2xl">
        Two independently reviewed LLM paper portfolios (mandate A and mandate B), each mirroring
        your real DKB holdings as their starting basis. Trigger a review to run a bounded LLM
        tool-loop that ends in a buy/sell/hold decision, gated by risk and portfolio checks —
        nothing here ever executes at DKB.
      </p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {MANDATES.map((m) => (
          <MandateCard key={m} mandate={m} portfolio={byMandate.get(m) ?? null} />
        ))}
      </div>
    </div>
  );
}
