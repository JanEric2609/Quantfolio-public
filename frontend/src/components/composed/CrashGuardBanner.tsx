import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import { getCrashGuard, type CrashGuardState } from "../../lib/api";
import { cn } from "../../lib/utils";

const TONE: Record<string, string> = {
  correction: "border-warn/40 bg-warn/10",
  bear: "border-danger/40 bg-danger/10",
  crash: "border-danger/60 bg-danger/15",
};

function pct(value: number) {
  return `${Math.round(value * 100)} %`;
}

/**
 * Shown on Home and This month while MSCI World is 10 % or more below its high,
 * or when a savings plan stopped or core units were sold during a fall. Calm
 * markets render nothing.
 */
export function CrashGuardBanner() {
  const guard = useQuery({ queryKey: ["plan", "crash-guard"], queryFn: getCrashGuard, staleTime: 15 * 60_000 });
  const data = guard.data;
  // A partial or failed answer renders nothing: this banner must never take the page down.
  if (!data?.market || !Array.isArray(data.behaviour) || !Array.isArray(data.episodes)) return null;
  if (!data.active && data.behaviour.length === 0) return null;
  return <CrashGuardCard data={data} />;
}

export function CrashGuardCard({ data }: { data: CrashGuardState }) {
  const { market } = data;
  const past = data.episodes.filter((e) => e.recovery_date !== null);
  return (
    <section
      aria-labelledby="crash-guard-title"
      className={cn("space-y-3 rounded-lg border p-4 text-sm", TONE[market.level] ?? "border-warn/40 bg-warn/10")}
    >
      <div className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
        <div className="space-y-1">
          <h2 id="crash-guard-title" className="text-base font-semibold text-text-primary">
            {data.active ? `${market.level_label}: markets are ${pct(market.drawdown)} below their high` : "Your plan needs a look"}
          </h2>
          <p className="text-text-secondary">{data.message}</p>
        </div>
      </div>

      {data.plan_note ? (
        <blockquote className="border-l-2 border-accent pl-3">
          <div className="text-xs font-medium uppercase tracking-wide text-text-muted">Your own rule, written in calm times</div>
          <div className="whitespace-pre-line text-text-primary">{data.plan_note}</div>
        </blockquote>
      ) : (
        <p className="text-text-secondary">
          Tip: write down your own rule for a fall in{" "}
          <Link to="/settings/profile" className="text-accent hover:underline">Profile</Link>; it is shown here when it matters.
        </p>
      )}

      {data.behaviour.length ? (
        <ul className="space-y-1.5">
          {data.behaviour.map((finding) => (
            <li key={finding.id}>
              <span className="font-medium text-text-primary">{finding.title}.</span>{" "}
              <span className="text-text-secondary">{finding.detail}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {past.length ? (
        <details className="group">
          <summary className="cursor-pointer text-text-secondary hover:text-text-primary">
            Earlier falls of 10 % or more since {market.history_start?.slice(0, 4) ?? "the start of the data"} ({past.length})
          </summary>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-xs tabular-nums">
              <thead className="text-left text-text-muted">
                <tr>
                  <th className="py-1 pr-3 font-medium">From the high of</th>
                  <th className="py-1 pr-3 font-medium">Deepest fall</th>
                  <th className="py-1 pr-3 font-medium">Months down</th>
                  <th className="py-1 font-medium">Months back to the high</th>
                </tr>
              </thead>
              <tbody>
                {past.map((e) => (
                  <tr key={e.peak_date} className="border-t border-border">
                    <td className="py-1 pr-3">{e.peak_date.slice(0, 7)}</td>
                    <td className="py-1 pr-3">{pct(e.depth)}</td>
                    <td className="py-1 pr-3">{Math.round(e.months_to_trough)}</td>
                    <td className="py-1">{e.months_to_recover !== null ? Math.round(e.months_to_recover) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
    </section>
  );
}
