import { AlertTriangle, BarChart3, ChevronDown, RefreshCw, TrendingUp } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useRegimeWeights } from "../hooks/useRegimeWeights";
import { TabNav } from "../../../components/composed/TabNav";
import { Button } from "../../../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "../../../components/ui/dropdown-menu";
import { cn } from "../../../lib/utils";

export interface QuantLabView {
  id: string;
  label: string;
}

/** The views in the tab row: your book, its risk, and the tools used most. */
export const PRIMARY_VIEWS: QuantLabView[] = [
  { id: "overview", label: "Overview" },
  { id: "holdings", label: "Holdings" },
  { id: "risk", label: "Risk" },
  { id: "optimisation", label: "Optimisation" },
  { id: "scenarios", label: "Monte Carlo" },
  { id: "backtest", label: "Backtest" },
  { id: "regime", label: "Regime" },
  { id: "runs", label: "Runs" },
];

/** Everything else, behind "More views". Every view keeps its URL. */
export const MORE_VIEW_GROUPS: { label: string; views: QuantLabView[] }[] = [
  {
    label: "Analysis",
    views: [
      { id: "correlation", label: "Correlation" },
      { id: "attribution", label: "Attribution" },
      { id: "factors", label: "Factor dashboard" },
      { id: "goals", label: "Goals" },
    ],
  },
  {
    label: "Research",
    views: [
      { id: "research", label: "Research library" },
      { id: "llm-research", label: "LLM research" },
      { id: "intelligence", label: "Market intelligence" },
    ],
  },
  {
    label: "Runs",
    views: [
      { id: "experiments", label: "Experiments" },
    ],
  },
];

const PRIMARY_TABS = PRIMARY_VIEWS.map((view) => ({ to: `/quantlab/${view.id}`, label: view.label }));

const REGIME_BG: Record<string, string> = {
  bull: "var(--c-regime-bull)",
  bear: "var(--c-regime-bear)",
  high_vol: "var(--c-regime-high_vol)",
  low_vol: "var(--c-regime-low_vol)",
  sideways: "var(--c-regime-sideways)",
  transition: "var(--c-regime-transition)",
};

const REGIME_TEXT: Record<string, string> = {
  bull: "text-success",
  bear: "text-danger",
  high_vol: "text-warn",
  low_vol: "text-sky-400",
  sideways: "text-text-secondary",
  transition: "text-text-secondary",
};

interface QuantLabHeaderProps {
  onRefresh: () => void;
}

function MoreViewsMenu({ currentId }: { currentId: string }) {
  const current = MORE_VIEW_GROUPS.flatMap((group) => group.views).find((view) => view.id === currentId);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-current={current ? "page" : undefined}
          className={cn(
            "flex min-h-11 shrink-0 items-center gap-1 whitespace-nowrap border-b-2 px-3 py-2 text-sm transition-colors sm:min-h-0",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1",
            current ? "border-accent text-accent" : "border-transparent text-text-secondary hover:text-text-primary",
          )}
        >
          {/* The size sits on the span: the global `button { font: inherit }` rule beats a size on the button. */}
          <span className="text-sm">{current ? current.label : "More views"}</span>
          <ChevronDown className="h-4 w-4 shrink-0" aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        {MORE_VIEW_GROUPS.map((group) => (
          <DropdownMenuGroup key={group.label}>
            <DropdownMenuLabel className="text-xs uppercase tracking-wide">{group.label}</DropdownMenuLabel>
            {group.views.map((view) => (
              <DropdownMenuItem key={view.id} asChild className={cn("min-h-11 sm:min-h-0", view.id === currentId && "text-accent")}>
                <Link to={`/quantlab/${view.id}`}>{view.label}</Link>
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function QuantLabHeader({
  onRefresh,
}: QuantLabHeaderProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const { data: regime } = useRegimeWeights();

  const segments = location.pathname.split("/").filter(Boolean);
  const viewId = segments.at(-1) ?? "overview";

  const regimeLabel = regime?.regime_label ?? "default";
  const regimeBg = REGIME_BG[regimeLabel] ?? "var(--c-regime-default)";
  const regimeTextClass = REGIME_TEXT[regimeLabel] ?? "text-text-secondary";

  return (
    <div className="lg:sticky lg:top-14 z-10 border-b border-line bg-bg/95 backdrop-blur" style={{ borderBottomWidth: 0 }}>
      {/* Row 1 — Regime + Controls */}
      <div
        className="flex items-center gap-3 px-4"
        style={{
          height: 48,
          minHeight: 48,
          borderLeft: `3px solid rgb(var(--c-accent))`,
          background: regimeBg,
        }}
      >
        {regime && (
          <div className={`flex items-center gap-2 text-xs ${regimeTextClass}`}>
            {regime.crisis ? (
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            ) : (
              <TrendingUp className="h-3.5 w-3.5 shrink-0" />
            )}
            <span className="font-medium uppercase" title="Market state from the statistical jump model">
              {regimeLabel}
            </span>
            {regime.state_since && (
              <span className="ml-1 text-[10px] opacity-60">since {regime.state_since}</span>
            )}
            {regime.crisis && <span className="ml-1 text-danger/80 text-[10px]">Crisis</span>}
          </div>
        )}

        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate("/portfolio/holdings")}
            className="gap-1 text-xs h-7"
            title="View Portfolio"
          >
            <BarChart3 className="h-3.5 w-3.5" />
            Portfolio
          </Button>
          <Button
            size="icon"
            variant="ghost"
            title="Refresh view data"
            aria-label="Refresh view data"
            onClick={onRefresh}
            className="h-7 w-7"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {/* Row 2 — one tab row: the eight main views, the rest under "More views". */}
      <div className="flex items-stretch border-b border-border px-2">
        <TabNav
          tabs={PRIMARY_TABS}
          ariaLabel="Quant Lab views"
          className="mx-0 min-w-0 flex-1 border-b-0 px-0 lg:mx-0 lg:px-0"
        />
        <MoreViewsMenu currentId={viewId} />
      </div>
    </div>
  );
}
