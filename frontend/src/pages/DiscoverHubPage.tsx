import { formatDateTime } from "../lib/format";
import { useState, useCallback, useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Brain, FlaskConical, ListChecks, TrendingUp, SlidersHorizontal, Search, Play, XCircle } from "lucide-react";
import { PageHeader } from "../components/composed/PageHeader";
import { Card } from "../components/ui/card";
import { Tabs, TabsContent } from "../components/ui/tabs";
import { TabNav } from "../components/composed/TabNav";
import { Button } from "../components/ui/button";
import { RunTrigger } from "../components/discover/RunTrigger";
import { StageProgress } from "../components/discover/StageProgress";
import { ShortlistCards } from "../components/discover/ShortlistCards";
import { useTrustRanking } from "./trust/useTrust";
import { RejectedTable } from "../components/discover/RejectedTable";
import { DossierDrawer } from "../components/discover/DossierDrawer";
import { SkillTrendPanel } from "../components/discover/SkillTrendPanel";
import { ConfigsTab } from "../components/discover/ConfigsTab";
import {
  api,
  type DiscoverRunSummary,
  type DiscoverRunDetail,
  type DiscoverCandidate,
  type DiscoverDossier,
  parseConcerns,
} from "../lib/api";

const DISCOVER_TABS = [
  { param: "funnel", label: "Funnel", icon: Brain },
  { param: "shortlist", label: "Shortlist & Dossiers", icon: ListChecks },
  { param: "track-record", label: "Track Record", icon: TrendingUp },
  { param: "configs", label: "Configs", icon: SlidersHorizontal },
];

export function DiscoverHubPage() {
  const queryClient = useQueryClient();
  // Measured hit rates per score tier, for the shortlist cards (ADR 0018 §6).
  const rankingQuery = useTrustRanking();
  const tiers = rankingQuery.data?.live.tiers ?? null;
  const [searchParams] = useSearchParams();
  const tab = searchParams.get("tab") ?? "funnel";

  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<DiscoverRunDetail | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [dossierOpen, setDossierOpen] = useState(false);
  const [selectedCandidate, setSelectedCandidate] = useState<DiscoverCandidate | null>(null);
  const [dossier, setDossier] = useState<DiscoverDossier | null>(null);
  const [isLoadingDossier, setIsLoadingDossier] = useState(false);

  const profileQuery = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<{ settings: Record<string, any> }>("/api/settings"),
  });

  const runsQuery = useQuery({
    queryKey: ["discover-runs"],
    queryFn: () => api<DiscoverRunSummary[]>("/api/discover/runs"),
  });

  const profile = profileQuery.data?.settings ?? null;

  const runStatus = activeRunId
    ? runsQuery.data?.find((r) => r.id === activeRunId)?.status ?? null
    : null;

  const handleCancel = async (mode: "graceful" | "immediate") => {
    if (!activeRunId) return;
    setIsCancelling(true);
    try {
      await api<{ status: string; mode: string }>(
        `/api/discover/runs/${activeRunId}/cancel?mode=${mode}`,
        { method: "POST" }
      );
      queryClient.invalidateQueries({ queryKey: ["discover-runs"] });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to cancel run");
    } finally {
      setIsCancelling(false);
    }
  };

  useEffect(() => {
    if (!runsQuery.data || activeRunId) return;
    const runningRuns = runsQuery.data.filter((r) => r.status === "running");
    if (runningRuns.length === 0) return;
    const latestRunning = runningRuns.reduce((latest, r) =>
      new Date(r.created_at) > new Date(latest.created_at) ? r : latest
    );
    setActiveRunId(latestRunning.id);
  }, [runsQuery.data, activeRunId]);

  // Candidates must survive a reload: with no in-flight run, default to the
  // latest finished run so the page never opens empty. A successfully
  // completed run is always preferred over a cancelled/failed one, even if
  // the cancelled/failed run is more recent — a cancellation (e.g. a deploy
  // restart racing an in-flight run) can leave a newer row on top that
  // carries no useful shortlist, which would otherwise shadow a perfectly
  // good older run.
  useEffect(() => {
    if (!runsQuery.data || activeRunId || selectedRunId) return;
    const pickLatest = (runs: DiscoverRunSummary[]) =>
      runs.reduce((best, r) =>
        new Date(r.created_at) > new Date(best.created_at) ? r : best
      );
    const completed = runsQuery.data.filter((r) => r.status === "completed");
    if (completed.length > 0) {
      setSelectedRunId(pickLatest(completed).id);
      return;
    }
    const finished = runsQuery.data.filter((r) =>
      ["cancelled", "failed"].includes(r.status)
    );
    if (finished.length === 0) return;
    setSelectedRunId(pickLatest(finished).id);
  }, [runsQuery.data, activeRunId, selectedRunId]);

  // Load the selected run's persisted candidates.
  useEffect(() => {
    if (!selectedRunId) return;
    let stale = false;
    api<DiscoverRunDetail>(`/api/discover/runs/${selectedRunId}`)
      .then((detail) => {
        if (!stale) setRunDetail(detail);
      })
      .catch((error) => {
        if (!stale) toast.error(error instanceof Error ? error.message : "Failed to load run");
      });
    return () => {
      stale = true;
    };
  }, [selectedRunId]);

  const handleStart = async (description: string) => {
    setIsStarting(true);
    try {
      const result = await api<{ run_id: string; status: string }>("/api/discover/runs", {
        method: "POST",
        body: JSON.stringify({ description: description || undefined }),
      });
      setActiveRunId(result.run_id);
      setRunDetail(null);
      setSelectedRunId(null);
      queryClient.invalidateQueries({ queryKey: ["discover-runs"] });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to start discovery run");
    } finally {
      setIsStarting(false);
    }
  };

  const handleComplete = useCallback((detail: DiscoverRunDetail) => {
    setRunDetail(detail);
    setSelectedRunId(detail.id);
    setActiveRunId(null);
    queryClient.invalidateQueries({ queryKey: ["discover-runs"] });
  }, [queryClient]);

  const handleError = useCallback((message: string) => {
    // StageProgress already shows a toast, avoid duplicate
    queryClient.invalidateQueries({ queryKey: ["discover-runs"] });
    setActiveRunId(null);
  }, [queryClient]);

  const handleOpenDossier = async (candidate: DiscoverCandidate) => {
    if (!candidate.dossier_id) {
      toast.info("Dossier not yet generated");
      return;
    }

    setSelectedCandidate(candidate);
    setDossierOpen(true);
    setDossier(null);
    setIsLoadingDossier(true);

    try {
      const data = await api<DiscoverDossier>(`/api/discover/dossiers/${candidate.dossier_id}`);
      setDossier(data);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to load dossier");
    } finally {
      setIsLoadingDossier(false);
    }
  };

  const shortlistedCands = runDetail?.shortlisted ?? [];
  const rejectedCands = runDetail?.rejected ?? [];
  const allCandidates = [...shortlistedCands, ...rejectedCands];
  const hasResults = allCandidates.length > 0;

  return (
    <div className="space-y-6">
      <PageHeader title="Discover" subtitle="Idea funnel, shortlist, and track record" />

      <div role="note" className="flex items-start gap-2 rounded-md border border-border bg-surface-2 px-3 py-2 text-sm text-text-secondary">
        <FlaskConical className="mt-0.5 h-4 w-4 shrink-0" />
        <span>
          <strong className="text-text-primary">Research only.</strong> These ideas have not passed the evidence gate
          and are not part of your plan. What to do with this month&apos;s money is on{" "}
          <Link to="/plan" className="text-accent underline-offset-2 hover:underline">This month</Link>.
        </span>
      </div>

      <Tabs value={tab}>
        <TabNav tabs={DISCOVER_TABS} ariaLabel="Discover sections" defaultParam="funnel" className="mb-4" />

        <TabsContent value="funnel">
          <div className="space-y-4">
            <Card className="p-6">
              <RunTrigger
                profile={profile}
                onStart={handleStart}
                isStarting={isStarting}
                activeRunId={activeRunId}
                runStatus={runStatus}
                onCancel={handleCancel}
                isCancelling={isCancelling}
              />
            </Card>
            {!activeRunId && (runsQuery.data?.length ?? 0) > 0 && (
              <Card className="flex flex-wrap items-center gap-x-3 gap-y-2 p-4">
                <span className="text-sm text-text-muted whitespace-nowrap">Run history</span>
                <select
                  className="min-w-0 w-full max-w-md rounded-md border border-border bg-surface-1 px-3 py-1.5 text-sm text-text-primary"
                  value={selectedRunId ?? ""}
                  onChange={(e) => {
                    setRunDetail(null);
                    setSelectedRunId(e.target.value || null);
                  }}
                >
                  {runsQuery.data!
                    .filter((r) => ["completed", "cancelled", "failed"].includes(r.status))
                    .map((r) => (
                      <option key={r.id} value={r.id}>
                        {formatDateTime(r.created_at)} — {r.status}
                      </option>
                    ))}
                </select>
              </Card>
            )}
            {activeRunId && (
              <StageProgress
                runId={activeRunId}
                onComplete={handleComplete}
                onError={handleError}
                onCancelled={(detail) => {
                  setRunDetail(detail);
                  setActiveRunId(null);
                  queryClient.invalidateQueries({ queryKey: ["discover-runs"] });
                }}
              />
            )}
            {hasResults && (
              <div className="space-y-4">
                <ShortlistCards candidates={allCandidates} onOpenDossier={handleOpenDossier} tiers={tiers} />
                <RejectedTable candidates={allCandidates} />
              </div>
            )}
            {runDetail?.status === "cancelled" && hasResults && (
              <Card className="border-danger/30 bg-danger/5 p-4">
                <p className="text-sm text-danger font-medium flex items-center gap-2">
                  <XCircle className="w-4 h-4" />
                  Run was cancelled — showing partial results below
                </p>
              </Card>
            )}
          </div>
        </TabsContent>

        <TabsContent value="shortlist">
          {hasResults ? (
            <ShortlistCards candidates={allCandidates} onOpenDossier={handleOpenDossier} tiers={tiers} />
          ) : (
            <Card className="flex flex-col items-center justify-center py-16 px-6 text-center space-y-3">
              <div className="rounded-full bg-surface-2 p-3">
                <ListChecks size={24} className="text-text-muted" />
              </div>
              <h3 className="text-sm font-semibold text-text-primary">Shortlist & Dossiers</h3>
              <p className="text-sm text-text-muted max-w-sm leading-relaxed">
                Run a discovery funnel to generate shortlist candidates and dossiers.
              </p>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="track-record">
          <SkillTrendPanel />
        </TabsContent>

        <TabsContent value="configs">
          <ConfigsTab />
        </TabsContent>
      </Tabs>

      <DossierDrawer
        open={dossierOpen}
        onClose={() => setDossierOpen(false)}
        dossier={dossier}
        symbol={selectedCandidate?.symbol ?? "—"}
        name={selectedCandidate?.name}
        tradeable={selectedCandidate?.tradeable}
        concerns={parseConcerns(selectedCandidate?.scores)}
      />
    </div>
  );
}
