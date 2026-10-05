import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type DriftWithTargetsResponse,
  type TargetAllocationResponse,
  type TargetAllocationItem,
} from "../../lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { Slider } from "../../components/ui/slider";
import { Label } from "../../components/ui/label";
import { Skeleton } from "../../components/ui/skeleton";
import { cn } from "../../lib/utils";
import { formatPercentPoints } from "../../lib/format";

const ASSET_LABELS: Record<string, string> = {
  equities: "Equities",
  bonds: "Bonds",
  cash: "Cash",
  crypto: "Crypto",
  real_estate: "Real Estate",
  commodities: "Commodities",
};

const ASSET_ORDER = ["equities", "bonds", "cash", "crypto", "real_estate", "commodities"];

function DriftBar({
  label,
  currentPct,
  targetPct,
  tolerance,
}: {
  label: string;
  currentPct: number;
  targetPct: number;
  tolerance: number;
}) {
  const drift = currentPct - targetPct;
  const absDrift = Math.abs(drift);
  const withinTolerance = absDrift <= tolerance;
  const colorClass = withinTolerance
    ? "text-success"
    : drift > 0
      ? "text-danger"
      : "text-warn";

  const maxWidth = Math.max(currentPct, targetPct, 10);

  return (
    <div className="space-y-1">
      <div className="flex flex-wrap justify-between gap-x-3 text-sm">
        <span className="text-text-primary font-medium">{label}</span>
        <span className={cn("font-mono text-xs", colorClass)}>
          {formatPercentPoints(currentPct, { digits: 1 })} vs target {formatPercentPoints(targetPct, { digits: 1 })}
          {drift !== 0 && (
            <span className="ml-1">
              ({formatPercentPoints(drift, { digits: 1, signed: true })})
            </span>
          )}
        </span>
      </div>
      <div className="relative h-5 bg-surface-2 rounded-full overflow-hidden">
        {/* Current bar */}
        <div
          className="absolute top-0 left-0 h-full bg-accent/60 rounded-full transition-all"
          style={{ width: `${(currentPct / maxWidth) * 100}%` }}
        />
        {/* Target marker */}
        <div
          className="absolute top-0 h-full border-r-2 border-dashed border-text-primary transition-all"
          style={{ left: `${(targetPct / maxWidth) * 100}%` }}
        />
      </div>
    </div>
  );
}

function EditTargetForm({
  targets,
  onSave,
  onCancel,
}: {
  targets: TargetAllocationItem[];
  onSave: (items: { asset_type: string; target_pct: number; tolerance_pct: number }[]) => void;
  onCancel: () => void;
}) {
  const [edits, setEdits] = useState<
    Record<string, { target_pct: number; tolerance_pct: number }>
  >(() => {
    const initial: Record<string, { target_pct: number; tolerance_pct: number }> = {};
    for (const t of targets) {
      initial[t.asset_type] = { target_pct: t.target_pct, tolerance_pct: t.tolerance_pct };
    }
    for (const at of ASSET_ORDER) {
      if (!initial[at]) {
        initial[at] = { target_pct: 0, tolerance_pct: 5 };
      }
    }
    return initial;
  });

  // Sync edits when targets data changes (e.g. after save)
  useEffect(() => {
    setEdits((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const t of targets) {
        if (
          next[t.asset_type]?.target_pct !== t.target_pct ||
          next[t.asset_type]?.tolerance_pct !== t.tolerance_pct
        ) {
          next[t.asset_type] = { target_pct: t.target_pct, tolerance_pct: t.tolerance_pct };
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [targets]);

  const total = Object.values(edits).reduce((s, v) => s + v.target_pct, 0);
  const needsNormalize = total > 0 && Math.abs(total - 100) > 0.5;

  const handleSave = () => {
    const items = ASSET_ORDER.map((at) => ({
      asset_type: at,
      target_pct: edits[at].target_pct,
      tolerance_pct: edits[at].tolerance_pct,
    }));
    onSave(items);
  };

  const handleNormalize = () => {
    const factor = 100 / total;
    const raw: Record<string, number> = {};
    for (const k of ASSET_ORDER) {
      raw[k] = edits[k].target_pct * factor;
    }
    const rounded: Record<string, number> = {};
    let sum = 0;
    for (const k of ASSET_ORDER) {
      rounded[k] = Math.round(raw[k]);
      sum += rounded[k];
    }
    // Distribute rounding remainder to the item with largest fractional part
    const diff = 100 - sum;
    if (diff !== 0) {
      const entries = ASSET_ORDER.map((k) => ({ key: k, frac: raw[k] - Math.floor(raw[k]) }));
      entries.sort((a, b) => b.frac - a.frac);
      rounded[entries[0].key] += diff;
    }
    setEdits((prev) => {
      const next = { ...prev };
      for (const k of ASSET_ORDER) {
        next[k] = { ...next[k], target_pct: rounded[k] };
      }
      return next;
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <span className="text-sm text-text-secondary">
          Total: <span className={cn("font-mono", Math.abs(total - 100) < 0.5 ? "text-success" : "text-danger")}>{formatPercentPoints(total, { digits: 1 })}</span>
          {needsNormalize && " (should be ~100%)"}
        </span>
      </div>
      {ASSET_ORDER.map((at) => (
        <div key={at} className="space-y-1">
          <div className="flex justify-between text-sm">
            <Label>{ASSET_LABELS[at] ?? at}</Label>
            <span className="font-mono text-xs text-text-secondary">{formatPercentPoints(edits[at].target_pct, { digits: 0 })}</span>
          </div>
          <Slider
            value={[edits[at].target_pct]}
            min={0}
            max={100}
            step={1}
            onValueChange={([v]) =>
              setEdits((prev) => ({ ...prev, [at]: { ...prev[at], target_pct: v } }))
            }
          />
          <div className="flex justify-between text-xs text-text-muted items-center">
            <span>Tolerance:</span>
            <div className="flex gap-1 items-center">
              <Button
                variant="ghost"
                size="sm"
                className="h-11 w-11 p-0 sm:h-5 sm:w-5"
                onClick={() => {
                  setEdits((prev) => ({
                    ...prev,
                    [at]: { ...prev[at], tolerance_pct: Math.max(prev[at].tolerance_pct - 1, 0) },
                  }));
                }}
                aria-label={`Decrease ${ASSET_LABELS[at] ?? at} tolerance`}
              >
                −
              </Button>
              <span className="min-w-[2ch] text-center font-mono">{formatPercentPoints(edits[at].tolerance_pct, { digits: 0 })}</span>
              <Button
                variant="ghost"
                size="sm"
                className="h-11 w-11 p-0 sm:h-5 sm:w-5"
                onClick={() => {
                  setEdits((prev) => ({
                    ...prev,
                    [at]: { ...prev[at], tolerance_pct: Math.min(prev[at].tolerance_pct + 1, 20) },
                  }));
                }}
                aria-label={`Increase ${ASSET_LABELS[at] ?? at} tolerance`}
              >
                +
              </Button>
            </div>
          </div>
        </div>
      ))}
      <div className="flex gap-2 pt-2">
        <Button onClick={handleSave}>Save Targets</Button>
        <Button variant="outline" onClick={onCancel}>Cancel</Button>
        {needsNormalize && (
          <Button variant="ghost" onClick={handleNormalize}>
            Normalize
          </Button>
        )}
      </div>
    </div>
  );
}

export function DriftTab() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);

  // Drift endpoint returns current allocation mapped to target taxonomy + drift
  const drift = useQuery({
    queryKey: ["portfolio-drift"],
    queryFn: () => api<DriftWithTargetsResponse>("/api/portfolio/target-allocation/drift"),
  });

  const targets = useQuery({
    queryKey: ["target-allocation"],
    queryFn: () => api<TargetAllocationResponse>("/api/portfolio/target-allocation"),
  });

  const saveTargets = useMutation({
    mutationFn: (items: { asset_type: string; target_pct: number; tolerance_pct: number }[]) =>
      api<TargetAllocationResponse>("/api/portfolio/target-allocation", {
        method: "PUT",
        body: JSON.stringify(items),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["target-allocation"] });
      qc.invalidateQueries({ queryKey: ["portfolio-drift"] });
      setEditing(false);
    },
  });

  // Values from drift endpoint are already mapped to target taxonomy as percentages
  const byType = drift.data?.current ?? {};
  const targetMap: Record<string, TargetAllocationItem> = {};
  for (const t of targets.data?.allocations ?? []) {
    targetMap[t.asset_type] = t;
  }

  const hasTargets = Object.keys(targetMap).length > 0;

  if (drift.isLoading || targets.isLoading) {
    return (
      <div className="space-y-6">
        <Card><CardContent className="pt-6"><Skeleton className="h-60 w-full" /></CardContent></Card>
        <Card><CardContent className="pt-6"><Skeleton className="h-40 w-full" /></CardContent></Card>
      </div>
    );
  }

  if (drift.isError || targets.isError) {
    return (
      <div className="space-y-6">
        <Card>
          <CardContent className="flex items-center justify-between gap-3 pt-6">
            <div className="text-sm text-danger">Failed to load allocation data.</div>
            <Button variant="outline" size="sm" onClick={() => { drift.refetch(); targets.refetch(); }}>Retry</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Drift View */}
      <Card>
        <CardHeader className="pb-3 flex flex-row items-center justify-between">
          <CardTitle className="text-lg">Target vs Current</CardTitle>
          {!editing && (
            <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
              {hasTargets ? "Edit Targets" : "Set Targets"}
            </Button>
          )}
        </CardHeader>
        <CardContent>
          {editing ? (
            <EditTargetForm
              targets={targets.data?.allocations ?? []}
              onSave={(items) => saveTargets.mutate(items)}
              onCancel={() => setEditing(false)}
            />
          ) : Object.keys(byType).length > 0 && hasTargets ? (
            <div className="space-y-4">
              {ASSET_ORDER.map((at) => {
                const currentPct = byType[at] ?? 0;
                const target = targetMap[at];
                if (!target || (target.target_pct === 0 && currentPct === 0)) return null;
                return (
                  <DriftBar
                    key={at}
                    label={ASSET_LABELS[at] ?? at}
                    currentPct={currentPct}
                    targetPct={target.target_pct}
                    tolerance={target.tolerance_pct}
                  />
                );
              })}
            </div>
          ) : Object.keys(byType).length > 0 ? (
            <p className="text-sm text-text-secondary">
              No target allocation set. Click "Set Targets" to define your target allocation and see drift analysis.
            </p>
          ) : (
            <p className="text-sm text-text-secondary">
              No holdings yet — add holdings to see your allocation vs targets.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Current Allocation Summary */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-lg">Current Allocation</CardTitle>
        </CardHeader>
        <CardContent>
          {Object.keys(byType).length > 0 ? (
            <div className="space-y-2">
              {Object.entries(byType).map(([type, value]) => (
                <div key={type} className="flex justify-between items-center text-sm">
                  <span className="text-text-secondary capitalize">{type}</span>
                  <span className="font-mono font-semibold">
                    {formatPercentPoints(Number(value), { digits: 1 })}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-text-secondary">
              No holdings yet — add holdings to see your allocation.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
