import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  SlidersHorizontal,
  Loader2,
  Plus,
  Wand2,
  ChevronDown,
  ChevronRight,
  ArrowUpRight,
  Lock,
  AlertTriangle,
  RefreshCw,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Textarea } from "../ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui/table";
import {
  getConfigs,
  getConfigReviews,
  activateConfig,
  syncConfigToDefault,
  perturbConfigs,
  createConfig,
  triggerReview,
  type ConfigResponse,
  type ConfigReviewResponse,
} from "../../lib/api";
import { formatDate, formatDateTime, formatNumber } from "../../lib/format";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const STATUS_VARIANT: Record<ConfigResponse["status"], "success" | "info" | "warning" | "secondary" | "danger"> = {
  active: "success",
  champion: "info",
  challenger: "warning",
  retired: "secondary",
  failed: "danger",
};

const CONFIG_TYPE_LABEL: Record<string, string> = {
  signal_weights: "Signal Weights",
  prompt_template: "Prompt Template",
  universe: "Universe",
};

function typeLabel(configType: string): string {
  return CONFIG_TYPE_LABEL[configType] ?? configType;
}

// signal_weights is the only config_type under automated statistical review
// (Bonferroni + DSR, weekly Sunday 04:00 UTC — see config_review.py). Manual
// promotion of that type fights the automated loop, so both the "Manual Add"
// and "Promote" actions are gated for signal_weights only; universe and
// prompt_template stay fully manually editable.
const AUTO_REVIEWED_CONFIG_TYPE = "signal_weights";
const PROMOTE_DISABLED_TOOLTIP =
  "Signal weights are promoted automatically by the weekly statistical review " +
  "(Sundays 04:00 UTC). Manual promotion is disabled to avoid conflicting with that process.";

// Bundled defaults so Manual Add and the empty-state prefill something
// meaningful instead of "{}" — an empty signal_weights config silently
// falls back to defaults with no visible error, so a blank textarea reads
// as "this did nothing" rather than as a mistake.
const CONFIG_TYPE_TEMPLATE: Record<string, Record<string, unknown>> = {
  signal_weights: {
    weights: {
      // Same as the backend's DEFAULT_SIGNAL_WEIGHTS (discover/config.py).
      ic_icir: 0.10, analyst: 0.05, sentiment: 0.05, portfolio: 0.15, fundamentals: 0.15,
      momentum: 0.15, risk: 0.10, benchmark: 0.05, ml_signal: 0.05, estimate_revision: 0.05,
      insider_signal: 0.05,
    },
    threshold_ic_obs: 20,
  },
  prompt_template: {
    system_prompt: "You are a quantitative equity research analyst...",
    temperature: 0.2,
    max_tokens: 4096,
  },
  universe: {
    universe_max_size: 200,
    min_market_cap_eur: 250_000_000,
    min_avg_volume: 100_000,
  },
};

function metricsSummary(m: Record<string, unknown> | null): string {
  if (!m) return "—";
  const parts: string[] = [];
  if (m.hit_rate != null) parts.push(`HR: ${formatNumber(Number(m.hit_rate), { digits: 2 })}`);
  if (m.rank_ic != null) parts.push(`IC: ${formatNumber(Number(m.rank_ic), { digits: 3 })}`);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function LoadingSkeleton() {
  return (
    <div className="space-y-4">
      <Card className="p-6 space-y-3">
        <Skeleton className="h-5 w-48" />
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </Card>
      <Card className="p-6 space-y-3">
        <Skeleton className="h-5 w-36" />
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-full" />
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Weight bars — nine floats are the whole content of a signal_weights
// config; a labelled bar row reads at a glance where raw JSON does not.
// ---------------------------------------------------------------------------

function WeightBars({ weights }: { weights: Record<string, number> | undefined }) {
  if (!weights || Object.keys(weights).length === 0) {
    return (
      <p className="text-xs text-danger">
        No "weights" key in this config — it will silently fall back to defaults if activated.
      </p>
    );
  }
  const entries = Object.entries(weights).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(([, v]) => Math.abs(v)), 0.01);
  const total = entries.reduce((sum, [, v]) => sum + v, 0);
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium text-text-secondary">Signal Weights</p>
        <p className={`text-xs ${Math.abs(total - 1) > 0.02 ? "text-warn" : "text-text-muted"}`}>
          sums to {formatNumber(total, { digits: 2 })}
        </p>
      </div>
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center gap-2 text-xs">
          <span className="w-24 shrink-0 text-text-secondary">{key}</span>
          <div className="flex-1 h-2 rounded-full bg-surface-2 overflow-hidden">
            <div
              className="h-full bg-accent rounded-full"
              style={{ width: `${Math.max(0, Math.min(100, (Math.abs(value) / max) * 100))}%` }}
            />
          </div>
          <span className="w-10 shrink-0 text-right text-text-muted tabular-nums">
            {formatNumber(value, { digits: 2 })}
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Config row (expandable)
// ---------------------------------------------------------------------------

function ConfigRow({
  config,
  onActivate,
  isActivating,
  onSync,
  isSyncing,
}: {
  config: ConfigResponse;
  onActivate: () => void;
  isActivating: boolean;
  onSync: () => void;
  isSyncing: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <>
      <TableRow
        className="cursor-pointer transition-colors hover:bg-surface-2/50"
        onClick={() => setExpanded((v) => !v)}
      >
        <TableCell className="w-8">
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-text-muted" />
          ) : (
            <ChevronRight className="w-4 h-4 text-text-muted" />
          )}
        </TableCell>
        <TableCell className="font-medium">{config.version_label}</TableCell>
        <TableCell>
          <Badge variant="secondary" className="text-xs">
            {typeLabel(config.config_type)}
          </Badge>
        </TableCell>
        <TableCell>
          <div className="flex items-center gap-1.5">
            <Badge variant={STATUS_VARIANT[config.status]} className="text-xs">
              {config.status}
            </Badge>
            {config.is_stale && (
              <>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span onClick={(e) => e.stopPropagation()} className="inline-flex">
                      <Badge variant="warning" className="text-xs gap-1">
                        <AlertTriangle className="w-3 h-3" />
                        Stale
                      </Badge>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs">
                    The code-level default for this config type has changed since this
                    row was seeded or last approved. Sync to pull in the current default.
                  </TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-6 px-2 text-xs"
                      disabled={isSyncing}
                      onClick={(e) => {
                        e.stopPropagation();
                        onSync();
                      }}
                    >
                      {isSyncing ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : (
                        <RefreshCw className="w-3 h-3" />
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs">
                    Overwrite this config's payload with the current code default,
                    clearing the Stale badge.
                  </TooltipContent>
                </Tooltip>
              </>
            )}
          </div>
        </TableCell>
        <TableCell className="text-text-muted text-xs">{config.source}</TableCell>
        <TableCell className="text-text-muted text-xs">{formatDate(config.created_at)}</TableCell>
        <TableCell className="text-text-muted text-xs">{metricsSummary(config.metrics_json)}</TableCell>
        <TableCell className="text-right">
          {config.status === "active" && (
            <Badge variant="success" className="text-xs">
              Active
            </Badge>
          )}
          {config.status === "challenger" && config.config_type === AUTO_REVIEWED_CONFIG_TYPE && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span onClick={(e) => e.stopPropagation()} className="inline-block">
                  <Button variant="outline" size="sm" disabled className="cursor-not-allowed">
                    <Lock className="w-3 h-3 mr-1" />
                    Promote
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">{PROMOTE_DISABLED_TOOLTIP}</TooltipContent>
            </Tooltip>
          )}
          {config.status === "challenger" && config.config_type !== AUTO_REVIEWED_CONFIG_TYPE && (
            <Button
              variant="outline"
              size="sm"
              disabled={isActivating}
              onClick={(e) => {
                e.stopPropagation();
                onActivate();
              }}
            >
              {isActivating ? (
                <Loader2 className="w-3 h-3 mr-1 animate-spin" />
              ) : (
                <ArrowUpRight className="w-3 h-3 mr-1" />
              )}
              Promote
            </Button>
          )}
          {config.status === "champion" && (
            <Badge
              variant="info"
              className="text-xs"
              title={
                config.config_type === AUTO_REVIEWED_CONFIG_TYPE
                  ? "Previously active; the current active config beat it in the weekly automated review. Champions cannot be reactivated directly — the review promotes a new challenger, or you can seed one via Generate Perturbations."
                  : "Previously active; the current active config beat it in a review. Champions cannot be reactivated directly — promote a new challenger, or create one from this config's JSON via Manual Add."
              }
            >
              Champion
            </Badge>
          )}
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={8} className="bg-surface-2/30 p-0">
            <div className="px-6 py-4 space-y-3">
              {config.description && (
                <p className="text-sm text-text-secondary">{config.description}</p>
              )}
              {config.parent_config_id && (
                <p className="text-xs text-text-muted">
                  Parent ID: {config.parent_config_id}
                </p>
              )}
              {config.champion_at && (
                <p className="text-xs text-text-muted">
                  Champion since: {formatDateTime(config.champion_at)}
                </p>
              )}
              {config.config_type === "signal_weights" ? (
                <WeightBars weights={config.config_json.weights as Record<string, number> | undefined} />
              ) : (
                <div>
                  <p className="text-xs font-medium text-text-secondary mb-1">Config JSON</p>
                  <pre className="text-xs bg-surface-2 p-3 rounded-md overflow-auto max-h-48 border border-border">
                    {JSON.stringify(config.config_json, null, 2)}
                  </pre>
                </div>
              )}
              {config.metrics_json && (
                <div>
                  <p className="text-xs font-medium text-text-secondary mb-1">Metrics</p>
                  <pre className="text-xs bg-surface-2 p-3 rounded-md overflow-auto max-h-32 border border-border">
                    {JSON.stringify(config.metrics_json, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Perturb dialog
// ---------------------------------------------------------------------------

function PerturbDialog({
  open,
  onOpenChange,
  onSuccess,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
}) {
  const [configType, setConfigType] = useState("signal_weights");
  const [nVariants, setNVariants] = useState("5");
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async () => {
    setIsSubmitting(true);
    try {
      const n = Math.min(20, Math.max(1, parseInt(nVariants, 10) || 5));
      const result = await perturbConfigs(configType, n);
      toast.success(`Generated ${result.created.length} perturbation variant(s)`);
      onSuccess();
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to generate perturbations");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Generate Perturbations</DialogTitle>
          <DialogDescription>
            Create challenger variants from the current active config.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="text-sm text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 p-3 rounded border border-gray-200 dark:border-gray-800">
            <p className="font-medium mb-1">What are perturbations?</p>
            <p>
              Perturbations auto-generate challenger config variants to test against the active one in the self-improvement loop.
              For signal weights, it creates N copies with small random weight noise (renormalized); for prompt templates, it creates 4 fixed reasoning-style variants.
              Pick how many weight variants to generate (default 5).
            </p>
          </div>
          <div className="space-y-2">
            <Label>Config Type</Label>
            <Select value={configType} onValueChange={setConfigType}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="signal_weights">Signal Weights</SelectItem>
                <SelectItem value="prompt_template">Prompt Template</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>Number of Variants (1-20)</Label>
            <Input
              type="number"
              min={1}
              max={20}
              value={nVariants}
              onChange={(e) => setNVariants(e.target.value)}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={isSubmitting}>
            {isSubmitting ? (
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
            ) : (
              <Wand2 className="w-4 h-4 mr-2" />
            )}
            Generate
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Manual add dialog
// ---------------------------------------------------------------------------

function templateFor(configType: string, configs: ConfigResponse[]): string {
  const active = configs.find((c) => c.config_type === configType && c.status === "active");
  const base = active?.config_json ?? CONFIG_TYPE_TEMPLATE[configType] ?? {};
  return JSON.stringify(base, null, 2);
}

function ManualAddDialog({
  open,
  onOpenChange,
  onSuccess,
  configs,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
  configs: ConfigResponse[];
}) {
  // signal_weights is excluded here — it's under automated weekly statistical
  // review (see AUTO_REVIEWED_CONFIG_TYPE) and manual challenger creation for
  // that type is disabled to avoid feeding uncontrolled variants into the
  // automated promotion loop. Default to prompt_template instead.
  const [configType, setConfigType] = useState("prompt_template");
  const [versionLabel, setVersionLabel] = useState("");
  const [description, setDescription] = useState("");
  const [configJson, setConfigJson] = useState(() => templateFor("prompt_template", configs));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleTypeChange = (value: string) => {
    setConfigType(value);
    // Prefill from the currently active config of this type — starting from
    // "{}" is how a config gets created that silently no-ops when activated
    // (compute_weighted_composite falls back to defaults with no error).
    setConfigJson(templateFor(value, configs));
    setJsonError(null);
  };

  const handleSubmit = async () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(configJson);
      setJsonError(null);
    } catch {
      setJsonError("Invalid JSON");
      return;
    }

    setIsSubmitting(true);
    try {
      await createConfig({
        config_type: configType,
        config_json: parsed,
        version_label: versionLabel || undefined,
        description: description || undefined,
      });
      toast.success("Config created successfully");
      onSuccess();
      onOpenChange(false);
      // Reset form
      setConfigType("prompt_template");
      setVersionLabel("");
      setDescription("");
      setConfigJson(templateFor("prompt_template", configs));
      setJsonError(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to create config");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Manual Add Config</DialogTitle>
          <DialogDescription>
            Create a new configuration manually.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>Config Type</Label>
            <Select value={configType} onValueChange={handleTypeChange}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="prompt_template">Prompt Template</SelectItem>
                <SelectItem value="universe">Universe</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-text-muted">
              Prefilled from the active {typeLabel(configType).toLowerCase()} config — edit in place rather than starting blank.
            </p>
            <p className="text-xs text-text-muted">
              Signal weights aren't listed here — they're promoted automatically by the weekly
              statistical review. Use "Generate Perturbations" to seed challenger variants instead.
            </p>
          </div>
          <div className="space-y-2">
            <Label>Version Label (optional)</Label>
            <Input
              placeholder="e.g. v1.2-manual"
              value={versionLabel}
              onChange={(e) => setVersionLabel(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>Description (optional)</Label>
            <Input
              placeholder="Brief description of this config"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>Config JSON</Label>
            <Textarea
              className={`font-mono text-xs min-h-[140px] ${jsonError ? "border-danger" : ""}`}
              value={configJson}
              onChange={(e) => {
                setConfigJson(e.target.value);
                setJsonError(null);
              }}
            />
            {jsonError && <p className="text-xs text-danger">{jsonError}</p>}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={isSubmitting}>
            {isSubmitting && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
            Create Config
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Review row (expandable)
// ---------------------------------------------------------------------------

function ReviewRow({
  review,
  configMap,
}: {
  review: ConfigReviewResponse;
  configMap: Map<string, ConfigResponse>;
}) {
  const [expanded, setExpanded] = useState(false);
  const champion = configMap.get(review.champion_id);
  const promoted = review.promoted_id ? configMap.get(review.promoted_id) : null;

  return (
    <>
      <div
        className="flex items-center gap-4 px-4 py-3 cursor-pointer hover:bg-surface-2/50 transition-colors border-b border-border/50"
        onClick={() => setExpanded((v) => !v)}
      >
        <div className="w-5">
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-text-muted" />
          ) : (
            <ChevronRight className="w-4 h-4 text-text-muted" />
          )}
        </div>
        <span className="text-xs text-text-muted w-28 shrink-0">
          {formatDateTime(review.reviewed_at)}
        </span>
        <Badge variant="secondary" className="text-xs shrink-0">
          {typeLabel(review.config_type)}
        </Badge>
        <span className="text-sm text-text-primary truncate">
          {champion?.version_label ?? review.champion_id.slice(0, 8)}
        </span>
        {promoted ? (
          <span className="flex items-center gap-1 text-sm text-success">
            <ArrowUpRight className="w-3 h-3" />
            {promoted.version_label}
          </span>
        ) : (
          <span className="text-xs text-text-muted">—</span>
        )}
      </div>
      {expanded && (
        <div className="px-4 py-3 bg-surface-2/30 border-b border-border/50 space-y-2">
          <p className="text-xs text-text-muted">
            Champion ID: {review.champion_id}
          </p>
          {review.promoted_id && (
            <p className="text-xs text-text-muted">
              Promoted ID: {review.promoted_id}
            </p>
          )}
          {review.rejected_ids.length > 0 && (
            <p className="text-xs text-text-muted">
              Rejected: {review.rejected_ids.length} config(s)
            </p>
          )}
          {review.challenger_results.length > 0 && (
            <div>
              <p className="text-xs font-medium text-text-secondary mb-1">Challenger Results</p>
              <pre className="text-xs bg-surface-2 p-3 rounded-md overflow-auto max-h-40 border border-border">
                {JSON.stringify(review.challenger_results, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ConfigsTab() {
  const queryClient = useQueryClient();
  const [perturbOpen, setPerturbOpen] = useState(false);
  const [manualAddOpen, setManualAddOpen] = useState(false);
  const [activatingId, setActivatingId] = useState<string | null>(null);
  const [syncingId, setSyncingId] = useState<string | null>(null);

  // --- Queries ---

  const configsQuery = useQuery({
    queryKey: ["discover-configs"],
    queryFn: () => getConfigs(),
  });

  const reviewsQuery = useQuery({
    queryKey: ["discover-config-reviews"],
    queryFn: getConfigReviews,
  });

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: ["discover-configs"] });
    queryClient.invalidateQueries({ queryKey: ["discover-config-reviews"] });
  };

  const handleActivate = async (id: string) => {
    setActivatingId(id);
    try {
      await activateConfig(id);
      toast.success("Config activated");
      invalidateAll();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to activate config");
    } finally {
      setActivatingId(null);
    }
  };

  const handleSync = async (id: string) => {
    setSyncingId(id);
    try {
      await syncConfigToDefault(id);
      toast.success("Config synced to current default");
      invalidateAll();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to sync config");
    } finally {
      setSyncingId(null);
    }
  };

  // --- Loading / Error states ---

  if (configsQuery.isLoading) return <LoadingSkeleton />;

  if (configsQuery.error) {
    return (
      <Card className="p-6">
        <p className="text-sm text-danger">Failed to load configurations.</p>
      </Card>
    );
  }

  const configs = configsQuery.data ?? [];
  const reviews = reviewsQuery.data ?? [];

  // Build lookup map for reviews
  const configMap = new Map<string, ConfigResponse>();
  for (const cfg of configs) {
    configMap.set(cfg.id, cfg);
  }

  // Group by type (known types first, in a stable order) so signal_weights,
  // prompt_template, and universe configs each read as their own section
  // instead of one flat list sorted only by created_at.
  const TYPE_ORDER = ["signal_weights", "prompt_template", "universe"];
  const STATUS_ORDER: Record<string, number> = { active: 0, champion: 1, challenger: 2, retired: 3, failed: 4 };
  const byType = new Map<string, ConfigResponse[]>();
  for (const cfg of configs) {
    const list = byType.get(cfg.config_type) ?? [];
    list.push(cfg);
    byType.set(cfg.config_type, list);
  }
  const configsByType: [string, ConfigResponse[]][] = [...byType.entries()]
    .sort(([a], [b]) => {
      const ia = TYPE_ORDER.indexOf(a);
      const ib = TYPE_ORDER.indexOf(b);
      return (ia === -1 ? TYPE_ORDER.length : ia) - (ib === -1 ? TYPE_ORDER.length : ib);
    })
    .map(([type, rows]) => [
      type,
      [...rows].sort((a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9)),
    ]);

  return (
    <div className="space-y-6">
      {/* Action bar */}
      <Card className="p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <SlidersHorizontal className="w-4 h-4 text-text-muted" />
            <h3 className="text-sm font-semibold text-text-primary">
              Configurations
            </h3>
            <Badge variant="secondary" className="text-xs">
              {configs.length}
            </Badge>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => setManualAddOpen(true)}>
              <Plus className="w-3.5 h-3.5 mr-1.5" />
              Manual Add
            </Button>
            <Button size="sm" onClick={() => setPerturbOpen(true)}>
              <Wand2 className="w-3.5 h-3.5 mr-1.5" />
              Generate Perturbations
            </Button>
          </div>
        </div>
      </Card>

      {/* Config table */}
      {configs.length === 0 ? (
        <Card className="flex flex-col items-center justify-center py-16 px-6 text-center space-y-3">
          <div className="rounded-full bg-surface-2 p-3">
            <SlidersHorizontal size={24} className="text-text-muted" />
          </div>
          <h3 className="text-sm font-semibold text-text-primary">No Configurations</h3>
          <p className="text-sm text-text-muted max-w-sm leading-relaxed">
            Generate perturbations or manually add a config to get started.
          </p>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>Version</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Metrics</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {configsByType.map(([type, rows]) => (
                <>
                  <TableRow key={`header-${type}`} className="hover:bg-transparent">
                    <TableCell colSpan={8} className="bg-surface-2/60 py-1.5 text-xs font-semibold text-text-secondary">
                      {typeLabel(type)}
                    </TableCell>
                  </TableRow>
                  {rows.map((cfg) => (
                    <ConfigRow
                      key={cfg.id}
                      config={cfg}
                      onActivate={() => handleActivate(cfg.id)}
                      isActivating={activatingId === cfg.id}
                      onSync={() => handleSync(cfg.id)}
                      isSyncing={syncingId === cfg.id}
                    />
                  ))}
                </>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {/* Review history */}
      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-sm font-semibold">Review History</CardTitle>
              <p className="text-xs text-text-muted mt-0.5">
                A challenger is promoted only once the champion has ≥30 resolved predictions
                and the challenger clears it on a significance-corrected score and calibration.
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={reviewsQuery.isLoading}
              onClick={async () => {
                try {
                  await triggerReview();
                  toast.success("Manual review triggered");
                  invalidateAll();
                } catch (error) {
                  toast.error(error instanceof Error ? error.message : "Failed to trigger review");
                }
              }}
            >
              <Wand2 className="w-3.5 h-3.5 mr-1.5" />
              Trigger Manual Review
            </Button>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {reviewsQuery.isLoading ? (
            <div className="px-4 py-3 space-y-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : reviews.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 px-6 text-center space-y-2">
              <p className="text-sm text-text-muted">No reviews yet.</p>
              <p className="text-xs text-text-muted">
                Trigger a manual review or wait for scheduled reviews.
              </p>
            </div>
          ) : (
            <div>
              {/* Header row */}
              <div className="flex items-center gap-4 px-4 py-2 text-xs font-medium text-text-secondary border-b border-border">
                <div className="w-5" />
                <span className="w-28 shrink-0">Date</span>
                <span className="shrink-0">Type</span>
                <span className="flex-1">Champion</span>
                <span className="shrink-0">Promoted</span>
              </div>
              {reviews.map((review) => (
                <ReviewRow key={review.id} review={review} configMap={configMap} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Dialogs */}
      <PerturbDialog
        open={perturbOpen}
        onOpenChange={setPerturbOpen}
        onSuccess={invalidateAll}
      />
      <ManualAddDialog
        open={manualAddOpen}
        onOpenChange={setManualAddOpen}
        onSuccess={invalidateAll}
        configs={configs}
      />
    </div>
  );
}
