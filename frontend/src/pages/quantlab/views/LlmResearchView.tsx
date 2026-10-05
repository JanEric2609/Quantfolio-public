import { formatPercent } from "../../../lib/format";
import { useLlmPropose, useLlmAnalyze } from "../hooks/useLlmResearch";
import { useObsidianSyncStatus } from "../hooks/useVerification";
import { SummaryStrip } from "../components/SummaryStrip";
import { LlmProse } from "../../../components/composed/LlmProse";
import { useState } from "react";

export function LlmResearchView() {
  const [focus, setFocus] = useState("general");
  const { data: proposals, refetch: fetchProposals, isFetching: proposing } = useLlmPropose(3);
  const { data: analysis, refetch: fetchAnalysis, isFetching: analyzing } = useLlmAnalyze(focus);
  const { data: syncStatus } = useObsidianSyncStatus();

  const syncing = syncStatus?.enabled ?? false;
  const hypothesisCount = syncStatus?.hypothesis_count ?? 0;

  return (
    <div className="space-y-6">
      <SummaryStrip
        metrics={[
          { label: "Proposals", value: proposals?.count != null ? String(proposals.count) : "—" },
          { label: "Confidence", value: analysis?.confidence != null ? formatPercent(analysis.confidence, { digits: 0 }) : "—" },
          { label: "Obsidian Sync", value: syncing ? `${hypothesisCount} notes` : "Off" },
        ]}
      />

      {/* Factor Proposals Section */}
      <div className="rounded-lg border border-border p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-foreground">LLM Factor Proposals</h3>
          <button
            onClick={() => fetchProposals()}
            disabled={proposing}
            className="px-3 py-1 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {proposing ? "Generating..." : "Propose Factors"}
          </button>
        </div>
        {proposals && proposals.proposals && proposals.proposals.length > 0 ? (
          <div className="space-y-3">
            {proposals.proposals.map((p, i: number) => (
              <div key={i} className="rounded-md border border-border/50 p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-medium text-foreground">{p.name}</span>
                  <div className="flex items-center gap-2">
                    {p.expected_regime && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground">
                        {p.expected_regime}
                      </span>
                    )}
                    <span className="text-[10px] text-muted-foreground">
                      {p.confidence ? formatPercent(p.confidence, { digits: 0 }) : "—"}
                    </span>
                  </div>
                </div>
                <p className="text-xs text-muted-foreground mb-1">{p.description}</p>
                <p className="text-[11px] text-muted-foreground/70 italic">{p.formula}</p>
                <p className="text-[11px] text-muted-foreground/70 mt-1">{p.intuition}</p>
              </div>
            ))}
            {proposals.reasoning && (
              <details className="text-[10px] text-muted-foreground/60">
                <summary className="cursor-pointer hover:text-muted-foreground">Show CoT reasoning</summary>
                <pre className="mt-1 whitespace-pre-wrap text-[10px] max-h-48 overflow-y-auto bg-muted/30 rounded p-2">
                  {proposals.reasoning.slice(0, 2000)}
                </pre>
              </details>
            )}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground/60">
            {proposing ? "Generating factor proposals via Financial CoT..." : "Click 'Propose Factors' to generate LLM-backed factor hypotheses."}
          </p>
        )}
      </div>

      {/* Market Analysis Section */}
      <div className="rounded-lg border border-border p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-foreground">Market Analysis</h3>
          <div className="flex items-center gap-2">
            <select
              value={focus}
              onChange={(e) => setFocus(e.target.value)}
              className="text-xs bg-muted border border-border rounded px-2 py-1"
            >
              <option value="general">General</option>
              <option value="regime">Regime Focus</option>
              <option value="factors">Factor Focus</option>
              <option value="portfolio">Portfolio Focus</option>
            </select>
            <button
              onClick={() => fetchAnalysis()}
              disabled={analyzing}
              className="px-3 py-1 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {analyzing ? "Analyzing..." : "Analyze"}
            </button>
          </div>
        </div>
        {analysis?.summary ? (
          <div className="space-y-3">
            <LlmProse text={analysis.summary} />
            <p className="text-xs text-muted-foreground italic">{analysis.regime_assessment}</p>
            {Boolean(analysis.risks && analysis.risks.length > 0) && (
              <div>
                <p className="text-[10px] font-medium text-foreground mb-1">Risks</p>
                <ul className="list-disc list-inside text-[11px] text-danger/80 space-y-0.5">
                  {(analysis.risks ?? []).map((r: string, i: number) => <li key={i}>{r}</li>)}
                </ul>
              </div>
            )}
            {Boolean(analysis.opportunities && analysis.opportunities.length > 0) && (
              <div>
                <p className="text-[10px] font-medium text-foreground mb-1">Opportunities</p>
                <ul className="list-disc list-inside text-[11px] text-success/80 space-y-0.5">
                  {(analysis.opportunities ?? []).map((o: string, i: number) => <li key={i}>{o}</li>)}
                </ul>
              </div>
            )}
            {Object.keys(analysis.factor_implications || {}).length > 0 && (
              <div>
                <p className="text-[10px] font-medium text-foreground mb-1">Factor Implications</p>
                <div className="grid grid-cols-2 gap-1">
                  {Object.entries(analysis.factor_implications || {}).map(([k, v]) => (
                    <div key={k} className="text-[10px]">
                      <span className="font-medium text-foreground">{k}:</span>{" "}
                      <span className="text-muted-foreground">{String(v)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground/60">
            {analyzing ? "Running Financial CoT analysis..." : "Select a focus and click 'Analyze' for LLM market analysis."}
          </p>
        )}
      </div>

      {/* Obsidian Sync Status */}
      <div className="rounded-lg border border-border p-4">
        <h3 className="text-sm font-semibold text-foreground mb-3">Obsidian Sync</h3>
        <div className="flex items-center gap-3">
          <div className={`w-2 h-2 rounded-full ${syncing ? "bg-success" : "bg-muted-foreground/30"}`} />
          <span className="text-xs text-muted-foreground">
            {syncing ? `${hypothesisCount} factor hypotheses in vault` : "Obsidian integration not configured"}
          </span>
        </div>
      </div>
    </div>
  );
}
