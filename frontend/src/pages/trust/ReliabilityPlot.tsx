import type { TrustTypeVerdict } from "../../lib/api";
import { Card } from "../../components/ui/card";
import { ScatterChart } from "../../components/charts/ScatterChart";
import { fmtPct } from "./trustFormat";

/**
 * "When we said X %, what happened?" One point per bin of stated probability
 * against the share that hit, with the diagonal as perfect calibration. Drawn
 * only when the backend sends bins (30 or more calls with a stated
 * probability); the table below the plot is the accessible version of it.
 */
export function ReliabilityPlot({ type }: { type: TrustTypeVerdict }) {
  const bins = type.reliability;
  if (bins.length === 0) return null;
  const points = bins.map((b) => [Math.round(b.p_mean * 1000) / 10, Math.round(b.hit_rate * 1000) / 10] as [number, number]);
  return (
    <Card className="space-y-3 p-4" data-testid={`reliability-${type.type}`}>
      <div>
        <h3 className="text-sm font-semibold text-text-primary">{type.label}: stated probability vs what happened</h3>
        {type.calibration_caption ? <p className="mt-1 text-sm text-text-secondary">{type.calibration_caption}</p> : null}
        <p className="text-xs text-text-muted">
          {type.n_stated_p} calls with a stated probability, in {bins.length} equal-count bins. Points on the diagonal
          mean the stated probability was right.
          {type.spiegelhalter_z != null ? ` Spiegelhalter Z ${type.spiegelhalter_z.toFixed(2)} (beyond ±1.96 means miscalibrated).` : ""}
        </p>
      </div>
      <ScatterChart
        ariaLabel={`${type.label} reliability diagram`}
        height={240}
        series={[{ name: "Bins", data: points }]}
        regression={{ slope: 1, intercept: 0, name: "Perfect calibration" }}
        regressionRange={[0, 100]}
        xName="Stated probability (%)"
        yName="Share that hit (%)"
      />
      <table className="w-full text-xs">
        <caption className="sr-only">{type.label} reliability bins</caption>
        <thead>
          <tr className="border-b border-border text-left text-text-muted">
            <th className="py-1 pr-3 font-normal">Said</th>
            <th className="py-1 pr-3 font-normal">Happened</th>
            <th className="py-1 text-right font-normal">Calls</th>
          </tr>
        </thead>
        <tbody>
          {bins.map((b, i) => (
            <tr key={i} className="border-b border-border/50 last:border-0">
              <td className="py-1 pr-3 font-mono tabular-nums">{fmtPct(b.p_mean)}</td>
              <td className="py-1 pr-3 font-mono tabular-nums">{fmtPct(b.hit_rate)}</td>
              <td className="py-1 text-right font-mono tabular-nums">{b.n}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
