import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { PageHeader } from "../components/composed/PageHeader";
import { getSignals, type SignalCell, type SignalVerdict, type SignalsDto } from "../lib/api";
import { verdictClass } from "../lib/signalVerdict";

const POLL_MS = 60_000;

const eur = (v: number | string | null | undefined, digits = 2): string => {
  if (v == null || v === "") return "-";
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "-";
};

function VerdictChip({ verdict }: { verdict: SignalVerdict }) {
  return <span data-testid="verdict" className={`inline-block rounded-full border px-2 py-0.5 text-xs ${verdictClass(verdict)}`}>{verdict}</span>;
}

function replayText(c: SignalCell): string {
  const r = c.replay;
  if (!r || !r.n) return "n/a";
  return `n ${r.n}, ${eur(r.mean_eur)} EUR a trade`;
}

function Bankroll({ b }: { b: SignalsDto["bankroll"] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Bankroll</CardTitle>
        <CardDescription>Your logged real trades against the stops. Order details stop when one is reached.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        <p>Month {eur(b.month_eur)} EUR of the {eur(b.monthly_stop, 0)} EUR loss stop; day {eur(b.day_eur)} EUR of {eur(b.daily_stop, 0)}.</p>
        <p>Open real positions {b.open_real} of {b.max_open}; the account is about {eur(b.pot, 0)} EUR.</p>
        {!b.ok && <p role="alert" className="text-red-600">Paper only: {b.reason}. Until {b.resumes}.</p>}
      </CardContent>
    </Card>
  );
}

function SignalsView({ data }: { data: SignalsDto }) {
  const events = data.cells.filter((c) => c.registration === "events");
  const first = data.cells.filter((c) => c.registration === "first");
  const table = (rows: SignalCell[]) => (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead><tr className="text-left"><th>Cell</th><th>Verdict</th><th>Live paper</th><th>Replay 2016-26</th><th>Your real trades</th></tr></thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.cell}>
              <td>{c.cell}</td>
              <td><VerdictChip verdict={c.verdict} /></td>
              <td>n {c.n}/{c.n_min}{c.mean_net != null ? `, ${eur(c.mean_net)} EUR a trade` : ""}</td>
              <td>{replayText(c)}</td>
              <td>{c.real ? `${c.real.n} trades, ${eur(c.real.real_total)} EUR (paper on the same: ${eur(c.real.paper_total_same_trades)})` : "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  return (
    <div className="space-y-6">
      <Bankroll b={data.bankroll} />
      <Card>
        <CardHeader><CardTitle>Open positions</CardTitle><CardDescription>Paper positions of the event desk.</CardDescription></CardHeader>
        <CardContent>
          {data.open_positions.length === 0 ? <p className="text-sm">No event position is open.</p> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead><tr className="text-left"><th>Instrument</th><th>Cell</th><th>Side</th><th>Entry</th><th>Stop</th><th>Now</th><th>Paper</th><th>Exit by</th></tr></thead>
                <tbody>
                  {data.open_positions.map((p) => (
                    <tr key={p.id}>
                      <td>{p.instrument}</td><td>{p.cell}</td><td>{p.direction}</td><td>{eur(p.entry_price)}</td>
                      <td>{eur(p.stop_price)}</td><td>{eur(p.last)}</td><td>{eur(p.paper_pnl_eur)} EUR</td>
                      <td>{p.max_exit_at ? new Date(p.max_exit_at).toLocaleDateString("de-DE") : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Cells</CardTitle>
          <CardDescription>Each cell is judged by its own pre-registered test (40 or more trades, Holm-corrected). Too early is the honest answer for months.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <h3 className="font-medium">Event desk (ADR 0028)</h3>
          {table(events)}
          <h3 className="font-medium">Index rule (ADR 0027)</h3>
          {table(first)}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Your trades</CardTitle><CardDescription>Your own record, real result beside the paper result of the same signal.</CardDescription></CardHeader>
        <CardContent>
          {data.journal.length === 0 ? <p className="text-sm">No logged trades yet.</p> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead><tr className="text-left"><th>Market</th><th>Cell</th><th>Status</th><th>Real / paper (EUR)</th></tr></thead>
                <tbody>
                  {data.journal.map((j) => (
                    <tr key={j.position_id}><td>{j.market_key}</td><td>{j.cell ?? "-"}</td><td>{j.status}</td>
                      <td>{eur(j.real_pnl_eur)} / {eur(j.paper_net_eur)}{j.rule === "other" ? " (your own rule, not counted in the cell)" : ""}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export function SignalsPage() {
  const [data, setData] = useState<SignalsDto | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    getSignals()
      .then((d) => { setData(d && Array.isArray(d.cells) ? d : null); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load the signals."));
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);
  return (
    <div className="space-y-6">
      <PageHeader title="Signals" subtitle="The event desk's cells: technical rules, hype, news and your own calls, scored on paper with the evidence beside them." />
      <div role="status" className="flex items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-4 py-3 text-sm font-medium">
        <ShieldAlert className="size-4" aria-hidden /> Paper cells, manual trades: nothing here places an order.
        <Link to="/desk/intraday" className="ml-auto underline">Intraday CFD</Link>
      </div>
      {error && <p className="text-sm text-red-500" role="alert">{error}</p>}
      {data ? <SignalsView data={data} /> : !error && <p className="text-sm">Loading.</p>}
    </div>
  );
}
