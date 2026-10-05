import { useQuery } from "@tanstack/react-query";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "../ui/tooltip";
import { getMacroRegime, type MacroRegimeLabel, type MacroRegimeSnapshot } from "../../lib/api";
import { formatDateTime, formatNumber, formatPercent, formatPercentPoints } from "../../lib/format";

type RegimeStyle = { bg: string; text: string; dot: string; label: string };

const REGIME_COLORS: Record<MacroRegimeLabel, RegimeStyle> = {
  bull:     { bg: "bg-success/15",   text: "text-success",        dot: "bg-success",   label: "Bull" },
  sideways: { bg: "bg-info/15",      text: "text-info",           dot: "bg-info",      label: "Sideways" },
  bear:     { bg: "bg-danger/15",    text: "text-danger",         dot: "bg-danger",    label: "Bear" },
  unknown:  { bg: "bg-slate-500/15", text: "text-text-secondary", dot: "bg-slate-400", label: "Unknown" },
};

/** Style for any label; one the table lacks must not throw in render. */
export function regimeStyle(label: string | null | undefined): RegimeStyle {
  const known = label ? REGIME_COLORS[label as MacroRegimeLabel] : undefined;
  if (known) return known;
  const text = label ? label.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "Unknown";
  return { bg: "bg-slate-500/15", text: "text-text-secondary", dot: "bg-slate-400", label: text };
}

function Row({ name, value }: { name: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-text-secondary">{name}</span>
      <span className="font-mono tabular-nums">{value}</span>
    </div>
  );
}

function RegimeTooltip({ snap }: { snap: MacroRegimeSnapshot }) {
  return (
    <div className="space-y-1 text-xs">
      <div className="text-[10px] text-text-tertiary pb-1 border-b border-border mb-1">
        Market state from the statistical jump model (MSCI World drawdown, VIX, credit spread), classified every
        morning. Every page uses this one model.
      </div>
      {snap.available ? (
        <>
          <Row name="In this state since" value={snap.state_since ?? "—"} />
          <Row name="VIX" value={formatNumber(snap.vix, { digits: 1 })} />
          <Row name="Credit spread" value={formatPercentPoints(snap.credit_spread, { digits: 2 })} />
          <Row name="Drawdown" value={formatPercent(snap.drawdown, { digits: 1 })} />
        </>
      ) : (
        <div className="text-text-secondary">{snap.reason ?? "Not classified yet."}</div>
      )}
      <div className="text-[10px] text-text-tertiary pt-1 border-t border-border mt-1">
        {snap.as_of ? `Data to ${snap.as_of} · ` : ""}classified {formatDateTime(snap.updated_at)}
      </div>
    </div>
  );
}

function RegimeDot({ style }: { style: RegimeStyle }) {
  return <span className={`inline-block w-1.5 h-1.5 rounded-full ${style.dot}`} />;
}

export function RegimeChip() {
  const { data: snap, isLoading, isError } = useQuery<MacroRegimeSnapshot>({
    queryKey: ["macro-regime"],
    queryFn: getMacroRegime,
    refetchInterval: 30 * 60 * 1000, // 30 min
    retry: 2,
  });

  if (isLoading || isError || !snap) {
    return null; // silently hide on loading/error
  }

  const colors = regimeStyle(snap.label);

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-medium leading-none ${colors.bg} ${colors.text} hover:opacity-80 transition-opacity`}
        >
          <RegimeDot style={colors} />
          {colors.label}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="w-56">
        <RegimeTooltip snap={snap} />
      </TooltipContent>
    </Tooltip>
  );
}
