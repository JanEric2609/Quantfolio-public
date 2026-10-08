import { useState } from "react";
import { XCircle } from "lucide-react";
import { formatCurrency, formatNumber, formatPercent } from "../../../lib/format";
import { Skeleton } from "../../../components/ui/skeleton";
import { EmptyState } from "../../../components/shared/EmptyState";
import { useQuantAllocator, useSaveAllocatorUniverse } from "../hooks/useQuantAllocator";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import type { NewMoneyPlan } from "../../../lib/api";

const pct = (v?: number | null, digits = 1) => formatPercent(v, { digits });
const eur = (v?: number | null) => (v == null ? "-" : formatCurrency(v, "EUR", { digits: 0 }));
const ORDER = ["min_variance", "erc", "hrp", "equal"];
const ISIN_RE = /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/;

const WHY: Record<string, string> = {
  min_variance: "Aims for the calmest mix; can lean heavily on one or two funds.",
  erc: "Every fund adds the same share of the book's risk (Maillard, Roncalli and Teiletche 2010).",
  hrp: "Groups similar funds first, then splits risk between the groups (López de Prado 2016).",
  equal: "Every fund the same weight. Hard to beat out of sample (DeMiguel, Garlappi and Uppal 2009).",
};

/**
 * How to split the next monthly contribution across a list of candidate ETFs.
 * Targets use only the covariance of EUR returns (no expected returns); the new
 * money goes to the most underweight candidate first and nothing is sold.
 */
export function AllocatorView() {
  const [cap, setCap] = useState<number | undefined>(undefined);
  const [amountText, setAmountText] = useState("");
  const [newIsin, setNewIsin] = useState("");
  const amountNum = Number.parseFloat(amountText.replace(",", "."));
  const amount = amountText.trim() !== "" && Number.isFinite(amountNum) && amountNum >= 0 ? amountNum : undefined;
  const alloc = useQuantAllocator({ amount, maxWeight: cap });
  const save = useSaveAllocatorUniverse();

  if (alloc.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  if (alloc.error) {
    return (
      <EmptyState
        icon={XCircle}
        title="Failed to load the allocator"
        description={alloc.error instanceof Error ? alloc.error.message : "An unexpected error occurred."}
      />
    );
  }
  const data = alloc.data;
  const universe = data?.universe ?? [];
  const isins = universe.map((u) => u.isin);
  const assets = data?.assets ?? [];
  const plans = ORDER.filter((k) => data?.plans?.[k]).map((k) => [k, data!.plans![k]] as [string, NewMoneyPlan]);
  const hist = data?.history;

  const addIsin = () => {
    const isin = newIsin.trim().toUpperCase();
    if (!ISIN_RE.test(isin) || isins.includes(isin)) return;
    save.mutate([...isins, isin], { onSuccess: () => setNewIsin("") });
  };
  const newIsinBad = newIsin.trim() !== "" && !ISIN_RE.test(newIsin.trim().toUpperCase());

  const metrics: MetricItem[] = [
    { label: "To invest", value: eur(data?.contribution_eur), hint: amount == null ? "monthly contribution setting" : "your amount" },
    { label: "Candidates", value: String(universe.length), hint: `${assets.length} with price history` },
    { label: "History", value: hist ? `${hist.days} days` : "-", hint: hist?.start ? `${hist.start} – ${hist.end}` : undefined },
  ];

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />
      <div className="space-y-4 pt-4">
        <section className="rounded-md p-3 text-xs bg-surface space-y-2">
          <h2 className="text-sm font-semibold text-text-primary">How should this month's money be split?</h2>
          <p className="text-text-muted">
            Pick the ETFs you would consider buying. For each way of choosing a target mix, the money goes to the funds
            furthest below target first. Nothing is sold. Only these candidates are counted, not the rest of your book
            (single stocks, cash).
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-text-muted">Amount (€)</span>
              <input
                className="w-24 rounded border border-line bg-surface px-2 py-1"
                inputMode="decimal"
                value={amountText}
                placeholder={data?.contribution_eur != null ? String(data.contribution_eur) : ""}
                onChange={(e) => setAmountText(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-text-muted">Cap per fund</span>
              <select
                className="rounded border border-line bg-surface px-2 py-1"
                value={cap ?? ""}
                onChange={(e) => setCap(e.target.value ? Number(e.target.value) : undefined)}
              >
                <option value="">none</option>
                <option value="0.8">80 %</option>
                <option value="0.6">60 %</option>
                <option value="0.4">40 %</option>
              </select>
            </label>
          </div>
          <ul className="flex flex-wrap gap-2" aria-label="Candidate ETFs">
            {universe.map((u) => (
              <li key={u.isin} className="flex items-center gap-2 rounded border border-line px-2 py-1" data-testid={`cand-${u.isin}`}>
                <span className="font-mono">{u.ticker ?? u.isin}</span>
                <span className="text-text-muted">{u.ticker ? u.isin : "no ticker found"}</span>
                {u.held_value_eur > 0 && <span className="text-text-muted">held {eur(u.held_value_eur)}</span>}
                <button
                  type="button"
                  aria-label={`Remove ${u.isin}`}
                  className="text-text-muted hover:text-danger"
                  disabled={save.isPending}
                  onClick={() => save.mutate(isins.filter((i) => i !== u.isin))}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
          <div className="flex items-center gap-2">
            <input
              className="w-44 rounded border border-line bg-surface px-2 py-1 font-mono uppercase"
              aria-label="ISIN to add"
              placeholder="IE00B4L5Y983"
              value={newIsin}
              onChange={(e) => setNewIsin(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addIsin()}
            />
            <button
              type="button"
              className="rounded border border-line px-2 py-1 hover:bg-surface-2"
              disabled={save.isPending || newIsin.trim() === "" || newIsinBad}
              onClick={addIsin}
            >
              Add ETF
            </button>
            {newIsinBad && <span className="text-warn">An ISIN has 12 characters, e.g. IE00B4L5Y983.</span>}
            {data?.universe_is_default && <span className="text-text-muted">Default list: the core ETFs you hold. Adding one saves your own list.</span>}
          </div>
        </section>

        {hist?.limited_by && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn" data-testid="history-warning">
            History: {hist.days} days, limited by {hist.limited_by}. A young fund shortens the sample for all candidates.
          </div>
        )}
        {data?.overlap_note && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn" data-testid="overlap-note">
            {data.overlap_note}
          </div>
        )}
        {(data?.unresolved?.length ?? 0) > 0 && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            No ticker found for {data!.unresolved!.join(", ")}; left out.
          </div>
        )}
        {(data?.missing_history?.length ?? 0) > 0 && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            No price history for {data!.missing_history!.join(", ")}; left out.
          </div>
        )}
        {!data?.available && (
          <div className="rounded-md p-3 text-sm text-text-secondary bg-surface">
            {data?.reason ?? "Price history is needed for the allocator."}
          </div>
        )}

        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {plans.map(([key, p]) => (
            <article key={key} className="rounded-md border border-line bg-panel p-3 text-sm" data-testid={`plan-${key}`}>
              <h3 className="font-semibold">{p.label}</h3>
              <p className="mt-0.5 text-[11px] text-text-muted">{WHY[key]}</p>
              <p className="mt-2 text-xs text-text-primary" data-testid={`summary-${key}`}>{p.summary}</p>
              <table className="mt-2 w-full text-xs">
                <thead className="text-text-muted">
                  <tr>
                    <th className="text-left font-normal">Fund</th>
                    <th className="text-right font-normal">Now</th>
                    <th className="text-right font-normal">Target</th>
                    <th className="text-right font-normal">This month</th>
                    <th className="text-right font-normal">In {p.months} months</th>
                  </tr>
                </thead>
                <tbody>
                  {assets.map((a) => (
                    <tr key={a}>
                      <td className="font-mono">{a}</td>
                      <td className="text-right font-mono tabular-nums">{pct(data?.weights_current?.[a])}</td>
                      <td className="text-right font-mono tabular-nums">{pct(p.target_weights[a])}</td>
                      <td className="text-right font-mono tabular-nums">{eur(p.eur_this_month[a])}</td>
                      <td className="text-right font-mono tabular-nums">{pct(p.weights_after[a])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 text-[11px] text-text-muted">
                Volatility of the target mix {pct(p.volatility)} a year.
              </p>
            </article>
          ))}
        </div>
        {Object.keys(data?.errors ?? {}).length > 0 && (
          <p className="text-xs text-warn">
            Not solved: {Object.entries(data!.errors!).map(([k, v]) => `${k} (${v})`).join("; ")}
          </p>
        )}
        <p className="text-xs text-text-muted">
          EUR daily returns, covariance shrunk toward a scaled identity (Ledoit-Wolf
          {data?.covariance ? `, intensity ${formatNumber(data.covariance.shrinkage, { digits: 2 })}` : ""}); no expected
          returns are used. "In {plans[0]?.[1].months ?? 12} months" assumes the same amount every month and ignores
          price moves. This is a what-if; your actual monthly plan follows its own settings (This month).
        </p>
      </div>
    </div>
  );
}
