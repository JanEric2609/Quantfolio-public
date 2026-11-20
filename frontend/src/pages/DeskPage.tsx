import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, OctagonX, ShieldAlert, Timer } from "lucide-react";
import { toast } from "sonner";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { PageHeader } from "../components/composed/PageHeader";
import { formatDateTime } from "../lib/format";
import {
  getDeskEvents, getDeskNews, getDeskPositions, getDeskScorecard, getDeskStatus, postDeskClose, postDeskHalt, postDeskIdea,
  postDeskResume, postDeskTake,
  type DeskEventDto, type DeskNewsDto, type DeskPositionDto, type DeskProposalDto, type DeskScorecardDto, type DeskStatusDto,
} from "../lib/api";

const LIMIT_LABELS: [string, string][] = [
  ["pot", "Pot (EUR)"], ["daily_loss", "Daily loss stop (EUR)"], ["max_loss_per_trade", "Max loss per trade at its stop (EUR)"],
  ["fee_per_order", "Fee per order (EUR)"], ["max_open_live", "Open positions"], ["max_entries_per_day", "Entries a day"],
  ["max_holding_trading_days", "Max trading days held"], ["warrant_min_days_to_expiry", "Warrant min days to expiry"],
  ["knockout_max_leverage", "Knock-out max leverage"], ["spread_cap_liquid", "Spread cap, liquid list"],
  ["spread_cap_other", "Spread cap, other"], ["max_quote_age_s", "Max quote age (s)"], ["tap_window_s", "Tap window (s)"],
];

/** Why a paper position was closed, in words. */
const EXIT_REASONS: Record<string, string> = {
  stop: "Stop-loss reached",
  knock_out: "Knocked out: the underlying touched the barrier (closed at 0)",
  horizon: "Holding time over",
  time: "Closing time (3 minutes before the close)",
  friday_cutoff: "21:30 cut-off before the weekend",
  holiday_cutoff: "21:30 cut-off before a holiday",
  quote_lost: "No fresh price for 10 minutes (closed at the last good price)",
  halt: "Desk halted",
  owner_close: "Closed by you",
};

/** The desk's background health, as the monitor and the news stream report it. */
function healthLines(health: Record<string, unknown> | undefined): string[] {
  const out: string[] = [];
  if (!health) return out;
  if (typeof health.last_error === "string" && health.last_error) {
    out.push(`Last market read failed (${health.last_error}). The desk retries every minute; exits are still checked.`);
  }
  if (typeof health.news_stream === "string" && health.news_stream !== "connected") {
    out.push(`Live news stream: ${health.news_stream.replace(/_/g, " ")}. It reconnects on its own.`);
  }
  return out;
}

const fmt = (v: number | null | undefined, digits = 2) => (v == null ? "-" : v.toFixed(digits));

/** Exactly what is held: the issuer's name, WKN (or ISIN) and the terms that decide the risk. */
function productTerms(p: DeskPositionDto): string {
  if (p.product_type === "cfd") return "Trading 212 CFD, manual";
  const lev = p.leverage_or_omega ? Number(p.leverage_or_omega).toFixed(1) : "?";
  const level = p.barrier_or_strike ? Number(p.barrier_or_strike).toLocaleString() : "?";
  const kind = p.product_type === "knockout" ? `leverage ${lev}, barrier ${level}` : `omega ${lev}, strike ${level}`;
  return `${p.wkn ? `WKN ${p.wkn}` : p.isin}, ${kind}, ${p.expiry ?? "open end"}`;
}

export function DeskPage() {
  const [status, setStatus] = useState<DeskStatusDto | null>(null);
  const [positions, setPositions] = useState<DeskPositionDto[]>([]);
  const [events, setEvents] = useState<DeskEventDto[]>([]);
  const [score, setScore] = useState<DeskScorecardDto | null>(null);
  const [news, setNews] = useState<DeskNewsDto[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [idea, setIdea] = useState("");
  const [proposals, setProposals] = useState<DeskProposalDto[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    Promise.all([getDeskStatus(), getDeskPositions(), getDeskEvents(100), getDeskScorecard(),
      getDeskNews(40).catch(() => [] as DeskNewsDto[])])
      .then(([s, p, e, c, n]) => {
        setNews(Array.isArray(n) ? n : []);
        // A partial payload (an error body, a stub) must not take the page down.
        setStatus(s && s.halt && s.limits && s.usage_today && s.chain ? s : null);
        setPositions(Array.isArray(p) ? p : []);
        setEvents(Array.isArray(e) ? e : []);
        setScore(c && c.cells ? c : null);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load the desk"));
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 30000);
    return () => window.clearInterval(t);
  }, [load]);

  async function act<T>(fn: () => Promise<T>, done?: (r: T) => string) {
    setBusy(true);
    try {
      const r = await fn();
      if (done) toast.success(done(r));
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  // The API answers 200 with closed=false when no fresh price could be read: that is not a close.
  async function closeOrThrow(positionId: string) {
    const r = await postDeskClose(positionId);
    if (!r.closed) throw new Error("Not closed: no fresh price. Try again in a minute.");
    return r;
  }

  const open = positions.filter((p) => p.status === "open");
  const closed = positions.filter((p) => p.status === "closed" && p.exit_reason !== "external")
    .sort((a, b) => (b.exit_at ?? "").localeCompare(a.exit_at ?? "")).slice(0, 10);
  const halted = status?.halt.halted ?? false;

  return (
    <div className="space-y-6">
      <PageHeader title="Derivatives desk" subtitle="Knock-outs and warrants on paper, with strict limits and a full log." />
      <div role="status" className="flex items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-4 py-3 text-sm font-medium">
        <ShieldAlert className="size-4" aria-hidden /> Paper trading: no real orders are placed
      </div>
      {error && <p className="text-sm text-red-500" role="alert">{error}</p>}

      {status && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Limits</CardTitle>
              <CardDescription>Fixed in code. Nothing here can be raised from the app.</CardDescription>
            </CardHeader>
            <CardContent>
              <table className="w-full text-sm"><tbody>
                {LIMIT_LABELS.map(([k, label]) => (
                  <tr key={k} className="border-b border-border/50"><td className="py-1">{label}</td>
                    <td className="py-1 text-right tabular-nums">{String(status.limits[k])}</td></tr>
                ))}
              </tbody></table>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Today</CardTitle>
              <CardDescription>What the live limits would have allowed (paper itself is not capped by them).</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <p>Entries {status.usage_today.entries} / {status.usage_today.max_entries} · Loss {status.usage_today.loss} / {status.usage_today.daily_loss} EUR ·
                Open {status.usage_today.open_live} / {status.usage_today.max_open_live} · Pot used {status.usage_today.pot_used} / {status.usage_today.pot} EUR</p>
              <p className="flex items-center gap-2">
                {status.chain.ok
                  ? <><CheckCircle2 className="size-4 text-green-500" aria-hidden /> Ledger chain verified</>
                  : <><OctagonX className="size-4 text-red-500" aria-hidden /> Ledger chain broken at event {status.chain.first_bad_seq}</>}
              </p>
              {typeof status.health?.last_tick_at === "string" && (
                <p className="text-text-secondary">Last check of open positions {formatDateTime(status.health.last_tick_at)}.</p>
              )}
              {healthLines(status.health).map((line) => <p key={line} className="text-amber-600">{line}</p>)}
              {status.health?.wrapper_outdated === true && (
                <p className="text-amber-600">The Scalable wrapper needs install.sh again before the desk can read quotes.</p>
              )}
              <div className="flex flex-wrap items-center gap-2">
                {halted
                  ? <Button disabled={busy} onClick={() => act(postDeskResume, () => "Desk resumed")}>Resume the desk</Button>
                  : <Button variant="destructive" disabled={busy} onClick={() => act(() => postDeskHalt("halted from the app"), () => "Desk halted")}>Halt the desk</Button>}
                {halted && <span className="text-red-500">Halted: {status.halt.reason ?? ""}</span>}
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>New idea</CardTitle>
          <CardDescription>
            Ticker or ISIN, up or down, optional: today or 1d (held at most until 21:30 on the next trading day), ko, warrant, both,
            and event (accept earnings in the holding time, or a US stock whose earnings date cannot be read). Confirm within 3 minutes of the proposal.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <form className="flex flex-wrap gap-2" onSubmit={(e) => {
            e.preventDefault();
            if (idea.trim()) act(() => postDeskIdea(idea.trim()), (r) => { setProposals(r.proposals); return "Proposals ready"; });
          }}>
            <Input aria-label="Idea" className="min-w-64 flex-1" placeholder="DAX up today ko" value={idea} onChange={(e) => setIdea(e.target.value)} />
            <Button type="submit" disabled={busy || !!halted}>Ask the desk</Button>
          </form>
          {proposals.map((p, i) => (
            <div key={i} className="rounded-md border border-border p-3 text-sm">
              <pre className="whitespace-pre-wrap font-sans">{p.message}</pre>
              {p.position && p.nonce && (
                <Button className="mt-2" disabled={busy} onClick={() => act(() => postDeskTake(p.position!.id, p.nonce!), (r) => {
                  // A re-ask (the price moved) answers with a new single-use code: the next tap must use it.
                  setProposals((cur) => cur.map((q, j) => (j !== i ? q
                    : { ...q, nonce: r.status === "reask" ? r.nonce : r.status === "no_quote" ? q.nonce : null,
                        position: r.position ?? q.position,
                        message: r.status === "reask" ? `${q.message}\n\n${r.message}` : q.message })));
                  return r.message;
                })}>
                  <Timer className="mr-1 size-4" aria-hidden /> Paper: take
                </Button>
              )}
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Open positions</CardTitle></CardHeader>
        <CardContent>
          {open.length === 0 ? <p className="text-sm text-text-secondary">None open.</p> : (
            <table className="w-full text-sm">
              <thead><tr className="text-left"><th>Product</th><th>Size</th><th>Entry</th><th>Stop</th><th>Last bid</th><th>Quote age</th><th /></tr></thead>
              <tbody>{open.map((p) => (
                <tr key={p.id} className="border-t border-border/50">
                  <td>{p.underlying} {p.direction} {p.product_type} <span className="text-text-secondary">{p.short_id}</span>
                    <div className="text-xs">{p.product_name ?? "-"}</div>
                    <div className="text-xs text-text-secondary">{productTerms(p)}</div></td>
                  <td>{p.product_type === "cfd" ? `${Number(p.quantity ?? 0).toFixed(4)} (${(Number(p.quantity ?? 0) * Number(p.entry_price ?? 0)).toFixed(0)} EUR)` : p.units}</td><td>{p.entry_price}</td><td>{p.stop_price}</td><td>{p.last_bid ?? "-"}</td>
                  <td>{p.last_quote_at ? `${Math.max(0, Math.round((Date.now() - new Date(p.last_quote_at).getTime()) / 1000))} s` : "-"}</td>
                  <td><Button size="sm" variant="outline" disabled={busy} onClick={() => act(() => closeOrThrow(p.id), () => "Closed")}>Close</Button></td>
                </tr>))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recently closed</CardTitle>
          <CardDescription>The last ten paper exits and why they happened. A knock-out that touched its barrier counts as a total loss.</CardDescription>
        </CardHeader>
        <CardContent>
          {closed.length === 0 ? <p className="text-sm text-text-secondary">Nothing closed yet.</p> : (
            <table className="w-full text-sm">
              <thead><tr className="text-left"><th>Product</th><th>Closed</th><th>Exit price</th><th>Net (EUR)</th><th>Why</th></tr></thead>
              <tbody>{closed.map((p) => (
                <tr key={p.id} className="border-t border-border/50">
                  <td>{p.underlying} {p.direction} {p.product_type} <span className="text-text-secondary">{p.short_id}</span></td>
                  <td>{p.exit_at ? formatDateTime(p.exit_at) : "-"}</td><td>{p.exit_price ?? "-"}</td>
                  <td className={Number(p.net_eur ?? 0) < 0 ? "text-red-500" : "text-green-600"}>{p.net_eur ?? "-"}</td>
                  <td>{EXIT_REASONS[p.exit_reason ?? ""] ?? (p.exit_reason ?? "-").replace(/_/g, " ")}</td>
                </tr>))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {score && (
        <Card>
          <CardHeader>
            <CardTitle>Scorecard</CardTitle>
            <CardDescription>
              Pre-registered rule {score.prereg_hash.slice(0, 12)}: n of {score.n_min}, mean net, Holm-adjusted p, placebo percentile.
              Cost share is the median of fees plus spread over notional.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <table className="w-full text-sm">
              <thead><tr className="text-left"><th>Cell</th><th>n / {score.n_min}</th><th>Mean net (EUR)</th><th>Hit rate</th><th>Cost share</th><th>p (Holm)</th><th>Placebo pct</th><th>Pass</th></tr></thead>
              <tbody>{Object.entries(score.cells).map(([cell, s]) => (
                <tr key={cell} className="border-t border-border/50">
                  <td>{cell}</td><td>{s.n} / {s.n_min}</td><td>{fmt(s.mean_net)}</td>
                  <td>{s.hit_rate == null ? "-" : `${(s.hit_rate * 100).toFixed(0)} %`}</td>
                  <td>{s.median_cost_share == null ? "-" : `${(s.median_cost_share * 100).toFixed(1)} %`}</td>
                  <td>{s.n >= s.n_min ? fmt(s.p_holm, 3) : "-"}</td><td>{fmt(s.placebo_percentile, 0)}</td>
                  <td>{s.passes ? "Yes" : "No"}</td>
                </tr>))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-text-secondary">Your own app trades are logged separately ({score.external_closed}) and never count here.</p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>News wire</CardTitle>
          <CardDescription>
            Alpaca stream, Fed, ECB, German ad-hoc disclosures, Handelsblatt and SEC 8-K filings of your holdings, rated 0-3 by the
            local model. A feed's very first read is kept as the starting point without alerts. Information only: the desk never trades on news.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {news.length === 0 ? <p className="text-sm text-text-secondary">Nothing received yet.</p> : (
            <ul className="max-h-96 space-y-1 overflow-y-auto text-sm">
              {news.map((n) => (
                <li key={n.id} className={n.alert === "sent" ? "font-medium" : ""}>
                  <span className="text-xs text-text-secondary">
                    {n.received_at ? formatDateTime(n.received_at) : ""} {n.source}
                    {n.materiality == null ? "" : ` ${n.materiality}/3 ${n.direction ?? ""}`}
                    {n.relevance ? ` ${n.relevance}` : ""}{n.alert === "sent" ? " sent" : n.alert === "baseline" ? " (first read, not sent)" : ""}
                  </span>{" "}
                  {n.url ? <a href={n.url} target="_blank" rel="noreferrer noopener" className="underline">{n.headline}</a> : n.headline}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Event log</CardTitle><CardDescription>Append-only. Newest last.</CardDescription></CardHeader>
        <CardContent>
          {events.length === 0 ? <p className="text-sm text-text-secondary">No events yet.</p> : (
            <ul className="max-h-96 space-y-1 overflow-y-auto text-xs">
              {events.map((e) => (
                <li key={e.seq} className="font-mono"><span className="text-text-secondary">#{e.seq} {e.created_at ? formatDateTime(e.created_at) : ""}</span> {e.kind}{" "}
                  {JSON.stringify(e.payload).slice(0, 160)}</li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
