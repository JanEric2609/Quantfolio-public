import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ShieldAlert } from "lucide-react";
import { toast } from "sonner";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { PageHeader } from "../components/composed/PageHeader";
import {
  getIntraday, postCfdJournal,
  type CfdJournalRow, type IntradayDto, type IntradayIndex, type T212Facts,
} from "../lib/api";
import { calcPosition, countdown } from "../lib/cfdCalc";
import { stacked } from "../lib/stackedTable";

const POLL_MS = 60_000;
const STALE_S = 600;

const num = (v: string | null | undefined): number | null => {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const fmt = (v: string | number | null | undefined, digits = 2): string => {
  const n = typeof v === "number" ? v : num(v);
  return n == null ? "-" : n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
};
const hhmm = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Berlin" }) : "-";

const SKIP_TEXT: Record<string, string> = {
  half_day: "half day", macro_event: "macro event today", cutoff_before_exit: "the 21:30 cut-off falls before the exit",
};

/** The direction DAX and Euro Stoxx 50 share today (both expected the same way, neither skipped), else null. */
function europeTwin(indices: IntradayIndex[]): string | null {
  const dax = indices.find((i) => i.key === "DAX"), es = indices.find((i) => i.key === "ES50");
  if (!dax?.direction || !es?.direction || dax.skip_reason || es.skip_reason) return null;
  return dax.direction === es.direction ? dax.direction : null;
}

function IndexCard({ idx, now }: { idx: IntradayIndex; now: number }) {
  const s = idx.session;
  const cfdToday = idx.cfd_today;
  const spread = num(idx.spread_points), typical = num(idx.typical_30m_points);
  const spreadShare = spread != null && typical != null && typical > 0 ? (spread / typical) * 100 : null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{idx.label}</CardTitle>
        <CardDescription>{idx.t212_name} · session {s.status}{s.open ? ` ${hhmm(s.open)}-${hhmm(s.close)}` : ""}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        {idx.skip_reason
          ? <p>No trade today: {SKIP_TEXT[idx.skip_reason] ?? idx.skip_reason}.</p>
          : <p>Entry {hhmm(idx.enter_at)} ({countdown(idx.enter_at, now)}), exit {hhmm(idx.exit_at)} ({countdown(idx.exit_at, now)})</p>}
        {idx.macro_events_today.length > 0 && <p>Events today: {idx.macro_events_today.join(", ").toUpperCase()}</p>}
        {!idx.skip_reason && <p>
          Expected: {idx.direction ? `${idx.direction} (r1 ${fmt(num(idx.r1) == null ? null : num(idx.r1)! * 100)} %)` : "not known yet"}
        </p>}
        {idx.error && (s.status === "before" && idx.error === "no_data"
          ? <p>Opens {hhmm(s.open)} Berlin; the numbers fill in once it trades.</p>
          : <p role="alert" className="text-amber-600">Reading failed: {idx.error.replace(/_/g, " ")}</p>)}
        {idx.index_level_est && (
          <table className="w-full"><tbody>
            <tr><td>Index level (estimate)</td><td className="text-right">{fmt(idx.index_level_est, 0)}</td></tr>
            <tr><td>Move since yesterday</td><td className="text-right">{fmt(idx.move_pct)} %</td></tr>
            <tr><td>1-day sigma</td><td className="text-right">{fmt(idx.sigma_points, 0)} pts</td></tr>
            <tr><td>Typical last-30-minute move</td><td className="text-right">±{fmt(idx.typical_30m_points, 0)} pts</td></tr>
            <tr><td>Spread (Trading 212, observed)</td><td className="text-right">{fmt(idx.spread_points, 1)} pts</td></tr>
            {spreadShare != null && (
              <tr><td>Spread as a share of the typical move</td><td className="text-right">{fmt(spreadShare, 1)} %</td></tr>
            )}
          </tbody></table>
        )}
        <p>
          Today's CFD: {cfdToday
            ? `${cfdToday.direction} ${cfdToday.status}${cfdToday.paper_pnl_before_costs ? `, paper ${fmt(cfdToday.paper_pnl_before_costs)} EUR before costs` : ""}`
              + `${cfdToday.paper_net_eur ? `, paper net ${fmt(cfdToday.paper_net_eur)} EUR` : ""}; your journal: ${cfdToday.journal_status ?? "-"}`
            : "none"}
        </p>
      </CardContent>
    </Card>
  );
}

function Calculator({ board, facts }: { board: IntradayDto["board"]; facts: T212Facts }) {
  const keys = ["DAX", "ES50", "SPX", "NDX"];
  const [key, setKey] = useState("DAX");
  const [direction, setDirection] = useState<"long" | "short">("long");
  const [risk, setRisk] = useState("5");
  const [stop, setStop] = useState("");
  const [level, setLevel] = useState("");
  const idx = board?.indices.find((i) => i.key === key);
  const usd = idx?.currency === "USD" || key === "SPX" || key === "NDX";
  const stopValue = stop !== "" ? Number(stop) : num(idx?.sigma_points) ?? Number.NaN;
  const levelValue = level !== "" ? Number(level) : num(idx?.index_level_est) ?? Number.NaN;
  const result = useMemo(() => calcPosition({
    direction, riskEur: Number(risk), stopPoints: stopValue, level: levelValue,
    eurPerPoint: usd ? (num(idx?.eur_per_unit) ?? Number.NaN) : 1,
    spreadBp: Number(facts.spread_bp[key] ?? 0), currency: usd ? "USD" : "EUR",
  }, {
    marginRate: Number(facts.margin_rate), overnightRate: Number(facts.overnight_rate),
    fxFee: Number(facts.fx_fee_on_result), minOrderEur: Number(facts.min_order_eur),
  }), [direction, risk, stopValue, levelValue, usd, idx?.eur_per_unit, facts, key]);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Position calculator</CardTitle>
        <CardDescription>Calculation only. Nothing is ordered; you place the trade by hand in Trading 212.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="grid gap-3 sm:grid-cols-5">
          <label className="space-y-1">Market
            <select className="block w-full rounded-md border bg-background px-2 py-2" value={key} onChange={(e) => { setKey(e.target.value); setStop(""); setLevel(""); }}>
              {keys.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          </label>
          <label className="space-y-1">Direction
            <select className="block w-full rounded-md border bg-background px-2 py-2" value={direction} onChange={(e) => setDirection(e.target.value as "long" | "short")}>
              <option value="long">long</option><option value="short">short</option>
            </select>
          </label>
          <label className="space-y-1">Risk (EUR)
            <Input inputMode="decimal" value={risk} onChange={(e) => setRisk(e.target.value)} />
          </label>
          <label className="space-y-1">Stop (points)
            <Input inputMode="decimal" placeholder={fmt(idx?.sigma_points, 0)} value={stop} onChange={(e) => setStop(e.target.value)} />
          </label>
          <label className="space-y-1">Level
            <Input inputMode="decimal" placeholder={fmt(idx?.index_level_est, 0)} value={level} onChange={(e) => setLevel(e.target.value)} />
          </label>
        </div>
        {result ? (
          <div className="space-y-1" data-testid="calc-result">
            <p>Quantity <strong>{result.quantity.toFixed(4)}</strong>, exposure {fmt(result.exposure)} EUR, margin {fmt(result.margin)} EUR</p>
            <p>Stop level {fmt(result.stopLevel, 1)}; spread cost about {fmt(result.spreadCost)} EUR; overnight about {fmt(result.overnightPerNight, 3)} EUR per night</p>
            {result.fxNote && <p>{result.fxNote}</p>}
            {result.belowMinimum && (
              <p role="alert" className="flex items-center gap-1 text-amber-600">
                <AlertTriangle className="size-4" aria-hidden /> Below the {fmt(facts.min_order_eur, 0)} EUR minimum order.
              </p>
            )}
          </div>
        ) : <p>Enter a risk, a stop and a level (the board fills them while it has data).</p>}
        <p className="text-muted-foreground">Trading 212 facts observed {facts.observed}.</p>
      </CardContent>
    </Card>
  );
}

function JournalTable({ rows, onChanged }: { rows: CfdJournalRow[]; onChanged: () => void }) {
  const [inputs, setInputs] = useState<Record<string, { level: string; quantity: string }>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (row: CfdJournalRow, action: "entered" | "skipped" | "closed") => {
    const v = inputs[row.position_id] ?? { level: "", quantity: "" };
    setBusy(row.position_id);
    try {
      await postCfdJournal(row.position_id, { action, level: v.level || null, quantity: action === "entered" ? v.quantity || null : null });
      toast.success(`Noted: ${action}.`);
      onChanged();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not save.");
    } finally {
      setBusy(null);
    }
  };
  if (rows.length === 0) return <p className="text-sm">No CFD signal yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className={stacked.table}>
        <thead className={stacked.thead}><tr className="text-left">
          <th>Date</th><th>Market</th><th>Suggested</th><th>Yours</th><th>In</th><th>Out</th><th>Slippage</th>
          <th>Real / paper EUR</th><th>Nights</th><th>Status</th><th>Your answer</th>
        </tr></thead>
        <tbody className={stacked.tbody}>
          {rows.map((r) => {
            const slip = num(r.entry_level) != null && num(r.index_level_est) != null
              ? (num(r.entry_level)! - num(r.index_level_est)!) * (r.direction === "long" ? 1 : -1) : null;
            const v = inputs[r.position_id] ?? { level: "", quantity: "" };
            return (
              <tr key={r.position_id} className={`${stacked.tr} border-t border-border/50 align-top`}>
                <td className={stacked.td} data-label="Date">{r.created_at ? new Date(r.created_at).toLocaleDateString("de-DE") : "-"}</td>
                <td className={stacked.td} data-label="Market">{r.market_key} {r.direction}</td>
                <td className={stacked.td} data-label="Suggested">{r.quantity_suggested ? Number(r.quantity_suggested).toFixed(4) : "-"}</td>
                <td className={stacked.td} data-label="Yours">{r.quantity ? Number(r.quantity).toFixed(4) : "-"}</td>
                <td className={stacked.td} data-label="In">{fmt(r.entry_level, 1)}</td>
                <td className={stacked.td} data-label="Out">{fmt(r.exit_level, 1)}</td>
                <td className={stacked.td} data-label="Slippage">{slip == null ? "-" : `${slip >= 0 ? "+" : ""}${slip.toFixed(1)} pts`}</td>
                <td className={stacked.td} data-label="Real / paper EUR">{fmt(r.real_pnl_eur)} / {fmt(r.paper_net_eur)}{r.rule === "other" ? " (your own rule)" : ""}</td>
                <td className={stacked.td} data-label="Nights">{r.nights_held ?? "-"}</td>
                <td className={stacked.td} data-label="Status">{r.status}</td>
                <td className={stacked.td} data-label="Your answer">
                  {(r.status === "sent" || r.status === "entered") && (
                    <div className="flex flex-wrap items-center gap-1">
                      <Input aria-label={`Level ${r.short_id}`} className="h-8 w-24" placeholder="level" inputMode="decimal"
                        value={v.level} onChange={(e) => setInputs({ ...inputs, [r.position_id]: { ...v, level: e.target.value } })} />
                      {r.status === "sent" && (
                        <Input aria-label={`Quantity ${r.short_id}`} className="h-8 w-24" placeholder="quantity" inputMode="decimal"
                          value={v.quantity} onChange={(e) => setInputs({ ...inputs, [r.position_id]: { ...v, quantity: e.target.value } })} />
                      )}
                      {r.status === "sent" && <Button size="sm" disabled={busy === r.position_id} onClick={() => act(r, "entered")}>Entered</Button>}
                      {r.status === "sent" && <Button size="sm" variant="outline" disabled={busy === r.position_id} onClick={() => act(r, "skipped")}>Skipped</Button>}
                      {r.status === "entered" && <Button size="sm" disabled={busy === r.position_id} onClick={() => act(r, "closed")}>Closed</Button>}
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function IntradayPage() {
  const [data, setData] = useState<IntradayDto | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(() => {
    getIntraday()
      .then((d) => { setData(d && d.t212 && Array.isArray(d.journal) ? d : null); setError(null); setNow(Date.now()); })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load the board."));
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);

  const board = data?.board ?? null;
  const stale = board != null && (data?.board_age_s ?? 0) > STALE_S;
  return (
    <div className="space-y-6">
      <PageHeader level={2} title="Intraday CFD" subtitle="The index rule's board, a position calculator and your journal of the manual Trading 212 trades." />
      <div role="status" className="flex items-center gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-4 py-3 text-sm font-medium">
        <ShieldAlert className="size-4" aria-hidden /> Paper rule, manual trades: nothing here places an order.
      </div>
      {error && <p className="text-sm text-red-500" role="alert">{error}</p>}
      {board ? (
        <>
          <p className="text-sm">As of {hhmm(board.as_of)} Berlin{data?.board_age_s != null ? ` (${Math.round(data.board_age_s / 60)} min ago)` : ""}.
            {stale && <span role="alert" className="ml-2 text-amber-600">The board is older than 10 minutes: the desk monitor may be stopped.</span>}
          </p>
          {europeTwin(board.indices) && (
            <p role="note" className="text-sm">
              DAX and Euro Stoxx 50 both say {europeTwin(board.indices)} today. They move almost together, so taking
              both is one bet at double the size. Take one if any; the DAX has the smaller spread.
            </p>
          )}
          <div className="grid gap-4 lg:grid-cols-2">
            {board.indices.map((i) => <IndexCard key={i.key} idx={i} now={now} />)}
          </div>
        </>
      ) : <p className="text-sm">The board is empty until the desk monitor has run (every five minutes in the trading hours).</p>}
      {data && <Calculator board={board} facts={data.t212} />}
      <Card>
        <CardHeader>
          <CardTitle>Your journal</CardTitle>
          <CardDescription>Your own record of the manual trades. It never changes the paper score.</CardDescription>
        </CardHeader>
        <CardContent>
          <JournalTable rows={data?.journal ?? []} onChanged={load} />
        </CardContent>
      </Card>
    </div>
  );
}
