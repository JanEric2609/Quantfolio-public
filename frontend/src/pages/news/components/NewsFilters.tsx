import { Input } from "../../../components/ui/input";
import { Label } from "../../../components/ui/label";
import { Switch } from "../../../components/ui/switch";

interface NewsFiltersProps {
  ticker?: string;
  sentiment?: string;
  source?: string;
  macroOnly?: boolean;
  relevance?: string;
  /** Distinct source names derived from the parent's news query. */
  availableSources?: string[];
  onChange: (updates: Record<string, string | undefined>) => void;
}

const SENTIMENTS = ["", "positive", "neutral", "negative"];
const RELEVANCE_OPTIONS = ["", "high", "medium", "low"];

export function NewsFilters({ ticker, sentiment, source, macroOnly, relevance, availableSources = [], onChange }: NewsFiltersProps) {
  return (
    <div className="flex flex-wrap items-end gap-4 rounded-md border border-border bg-surface-2 p-4">
      <div className="space-y-1">
        <Label className="text-xs text-text-secondary">Ticker</Label>
        <Input
          placeholder="Any ticker"
          value={ticker ?? ""}
          onChange={(e) => onChange({ ticker: e.target.value || undefined })}
          className="w-36"
        />
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-text-secondary">Sentiment</Label>
        <select
          className="h-10 rounded-md border border-border bg-surface-2 px-3 text-sm"
          value={sentiment ?? ""}
          onChange={(e) => onChange({ sentiment: e.target.value || undefined })}
        >
          {SENTIMENTS.map((s) => (
            <option key={s} value={s}>{s || "All"}</option>
          ))}
        </select>
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-text-secondary">Relevance</Label>
        <select
          className="h-10 rounded-md border border-border bg-surface-2 px-3 text-sm"
          value={relevance ?? ""}
          onChange={(e) => onChange({ relevance: e.target.value || undefined })}
        >
          {RELEVANCE_OPTIONS.map((r) => (
            <option key={r} value={r}>{r ? r.charAt(0).toUpperCase() + r.slice(1) : "All"}</option>
          ))}
        </select>
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-text-secondary">Source</Label>
        <select
          className="h-10 rounded-md border border-border bg-surface-2 px-3 text-sm"
          value={source ?? ""}
          onChange={(e) => onChange({ source: e.target.value || undefined })}
        >
          <option value="">All Sources</option>
          {availableSources.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>
      <div className="flex items-center gap-2 pb-1">
        <Switch
          id="macro-switch"
          checked={macroOnly}
          onCheckedChange={(v) => onChange({ macro: v ? "true" : undefined })}
        />
        <Label htmlFor="macro-switch" className="text-xs text-text-secondary">Macro only</Label>
      </div>
    </div>
  );
}
