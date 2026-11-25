import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { PageHeader } from "../components/composed/PageHeader";
import { getSignals, type SignalCell, type SignalVerdict, type SignalsDto } from "../lib/api";
import { verdictClass } from "../lib/signalVerdict";
import { stacked } from "../lib/stackedTable";

const POLL_MS = 60_000;

const eur = (v: number | string | null | undefined, digits = 2): string => {
  if (v == null || v === "") return "-";
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "-";
};

function VerdictChip({ verdict }: { verdict: SignalVerdict }) {
  return <span data-testid="verdict" className={`inline-block rounded-full border px-2 py-0.5 text-xs ${verdictClass(verdict)}`}>{verdict}</span>;
}

/** Plain names for the cell ids, shown beneath the id. */
const CELL_NAMES: Record<string, string> = {
  "ta_trend:cfd": "EMA cross", "ta_band:cfd": "Bollinger fade", "ta_rsi2:cfd": "RSI(2) dip in an uptrend",
  "hype_fade:cfd": "Fade a retail-hype spike", "news_drift:cfd": "Follow company news",
  "owner_call:cfd": "Your /idea calls on CFDs", "owner_call:knockout": "Your /idea calls on knock-outs",
  "owner_call:warrant": "Your /idea calls on warrants", "index_momentum:knockout": "Index rule on knock-outs",
  "index_momentum:warrant": "Index rule on warrants", "index_momentum:cfd": "Index rule on CFDs",
};

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
      <table className={stacked.table}>
        <thead className={stacked.thead}><tr className="text-left"><th>Cell</th><th>Verdict</th><th>Live paper</th><th>Replay 2016-26</th><th>Your real trades</th></tr></thead>
        <tbody className={stacked.tbody}>
          {rows.map((c) => (
            <tr key={c.cell} className={stacked.tr}>
              <td className={stacked.td} data-label="Cell">
                <span><span>{c.cell}</span>{CELL_NAMES[c.cell] && <span className="block text-xs text-muted-foreground">{CELL_NAMES[c.cell]}</span>}</span>
              </td>
              <td className={stacked.td} data-label="Verdict"><VerdictChip verdict={c.verdict} /></td>
              <td className={stacked.td} data-label="Live paper">n {c.n}/{c.n_min}{c.mean_net != null ? `, ${eur(c.mean_net)} EUR a trade` : ""}</td>
              <td className={stacked.td} data-label="Replay 2016-26">{replayText(c)}</td>
              <td className={stacked.td} data-label="Your real trades">{c.real ? `${c.real.n} trades, ${eur(c.real.real_total)} EUR (paper on the same: ${eur(c.real.paper_total_same_trades)})` : "-"}</td>
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
              <table className={stacked.table}>
                <thead className={stacked.thead}><tr className="text-left"><th>Instrument</th><th>Cell</th><th>Side</th><th>Entry</th><th>Stop</th><th>Now</th><th>Paper</th><th>Exit by</th></tr></thead>
                <tbody className={stacked.tbody}>
                  {data.open_positions.map((p) => (
                    <tr key={p.id} className={stacked.tr}>
                      <td className={stacked.td} data-label="Instrument">{p.instrument}</td><td className={stacked.td} data-label="Cell">{p.cell}</td><td className={stacked.td} data-label="Side">{p.direction}</td><td className={stacked.td} data-label="Entry">{eur(p.entry_price)}</td>
                      <td className={stacked.td} data-label="Stop">{eur(p.stop_price)}</td><td className={stacked.td} data-label="Now">{eur(p.last)}</td><td className={stacked.td} data-label="Paper">{p.paper_pnl_eur == null ? "-" : `${eur(p.paper_pnl_eur)} EUR`}</td>
                      <td className={stacked.td} data-label="Exit by">{p.max_exit_at ? new Date(p.max_exit_at).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short", timeZone: "Europe/Berlin" }) : "-"}</td>
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
          <h3 className="font-medium">Index rule and your knock-out / warrant calls (ADR 0027)</h3>
          {table(first)}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Your trades</CardTitle><CardDescription>Your own record, real result beside the paper result of the same signal.</CardDescription></CardHeader>
        <CardContent>
          {data.journal.length === 0 ? <p className="text-sm">No logged trades yet.</p> : (
            <div className="overflow-x-auto">
              <table className={stacked.table}>
                <thead className={stacked.thead}><tr className="text-left"><th>Market</th><th>Cell</th><th>Status</th><th>Real / paper (EUR)</th></tr></thead>
                <tbody className={stacked.tbody}>
                  {data.journal.map((j) => (
                    <tr key={j.position_id} className={stacked.tr}>
                      <td className={stacked.td} data-label="Market">{j.market_key}</td><td className={stacked.td} data-label="Cell">{j.cell ?? "-"}</td><td className={stacked.td} data-label="Status">{j.status}</td>
                      <td className={stacked.td} data-label="Real / paper (EUR)">{eur(j.real_pnl_eur)} / {eur(j.paper_net_eur)}</td></tr>
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
