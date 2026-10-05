import { useQuery } from "@tanstack/react-query";
import {
  api,
  type TaxAllowanceAction,
  type TaxAllowanceBank,
  type TaxAllowancePlan,
} from "../../../lib/api";
import { formatCurrency, formatDate, formatPercent } from "../../../lib/format";
import { Badge } from "../../../components/ui/badge";
import { cn } from "../../../lib/utils";

const SEVERITY_STYLE: Record<TaxAllowanceAction["severity"], string> = {
  action: "border-warn/40 bg-warn/10",
  warning: "border-danger/40 bg-danger/10",
  info: "border-border bg-surface-2",
};

const SEVERITY_LABEL: Record<TaxAllowanceAction["severity"], string> = {
  action: "To do",
  warning: "Warning",
  info: "Note",
};

const eur = (value: number | null | undefined) => formatCurrency(value, "EUR");

function Header() {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <div className="text-sm font-medium text-text-primary">Freistellungsauftrag &amp; NV certificate</div>
      <div className="text-[11px] text-text-secondary">Estimate · not tax advice</div>
    </div>
  );
}

function ActionItem({ action, bankLabel }: { action: TaxAllowanceAction; bankLabel: string | null }) {
  return (
    <li className={cn("rounded-md border p-3 text-xs", SEVERITY_STYLE[action.severity])}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="sr-only">{SEVERITY_LABEL[action.severity]}: </span>
        <span className="text-sm font-medium text-text-primary">{action.title}</span>
        {action.deadline ? (
          <span className="whitespace-nowrap text-text-secondary">by {formatDate(action.deadline)}</span>
        ) : null}
      </div>
      <p className={cn("mt-1", action.severity === "info" ? "text-text-secondary" : "text-text-primary")}>
        {action.message}
      </p>
      {action.how_to ? (
        <details className="mt-2 text-text-secondary">
          <summary className="cursor-pointer font-medium hover:text-text-primary">
            How to do it{bankLabel ? ` at ${bankLabel}` : ""}
          </summary>
          <p className="mt-1">{action.how_to}</p>
        </details>
      ) : null}
    </li>
  );
}

function BanksTable({ banks }: { banks: TaxAllowanceBank[] }) {
  const num = "px-3 py-2 text-right tabular-nums";
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full min-w-[44rem] text-xs">
        <thead className="border-b border-border bg-surface-2 text-left text-text-secondary">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">Bank</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Freistellungsauftrag on file</th>
            <th scope="col" className="px-3 py-2 font-medium">NV copy</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Booked so far</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Projected this year</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Withheld (projected)</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Suggested order</th>
          </tr>
        </thead>
        <tbody>
          {banks.map((b) => (
            <tr key={b.bank} className="border-b border-border last:border-b-0">
              <th scope="row" className="px-3 py-2 text-left font-medium text-text-primary">{b.label}</th>
              <td className={num}>
                {b.fsa_eur == null ? <span className="text-text-muted">not entered</span> : eur(b.fsa_eur)}
              </td>
              <td className="px-3 py-2">
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  <span>{b.nv_filed ? "Yes" : "No"}</span>
                  {b.nv_covers ? <Badge variant="success">covers</Badge> : null}
                </span>
              </td>
              <td className={num}>{eur(b.booked_eur)}</td>
              <td className={num}>{eur(b.projected_eur)}</td>
              <td className={num}>{eur(b.projected_withheld_eur)}</td>
              <td className={num}>
                {eur(b.recommended_fsa_eur)}
                {b.change_eur !== 0 ? (
                  <div className="text-[11px] text-text-secondary">{formatCurrency(b.change_eur, { currency: "EUR", signed: true })}</div>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function interestBasis(bank: TaxAllowanceBank): string {
  const { interest_basis: basis } = bank.expected;
  if (basis === "rate") {
    const parts = [
      bank.savings_balance_eur != null ? eur(bank.savings_balance_eur) : null,
      bank.interest_rate != null ? formatPercent(bank.interest_rate) : null,
    ];
    return parts.every(Boolean) ? `from balance × rate (${parts[0]} × ${parts[1]})` : "from balance × rate";
  }
  if (basis === "run_rate") return "from the run-rate so far";
  if (basis === "unknown") {
    return bank.bank === "dkb"
      ? "unknown — enter the DKB Tagesgeld rate in the tax settings"
      : "unknown — no interest rate for this account yet";
  }
  return "none expected";
}

function Line({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div>
      <div className="flex justify-between gap-3">
        <span className="text-text-secondary">{label}</span>
        <span className="tabular-nums text-text-primary">{value}</span>
      </div>
      {note ? <div className="text-[11px] text-text-secondary">{note}</div> : null}
    </div>
  );
}

function BankBreakdown({ bank }: { bank: TaxAllowanceBank }) {
  const vorab = bank.expected.vorabpauschale_eur;
  return (
    <div className="space-y-2 rounded-md bg-surface-2 p-3">
      <div className="text-xs font-medium text-text-primary">{bank.label}</div>
      <div className="space-y-0.5">
        <div className="font-medium text-text-primary">Booked so far</div>
        <Line label="Dividends" value={eur(bank.booked.dividends_eur)} />
        <Line label="Interest" value={eur(bank.booked.interest_eur)} />
        <Line label="Vorabpauschale" value={eur(bank.booked.vorabpauschale_eur)} />
        <Line label="Gains" value={eur(bank.booked.gains_eur)} />
        <Line label="Losses" value={eur(bank.booked.losses_eur)} />
      </div>
      <div className="space-y-0.5">
        <div className="font-medium text-text-primary">Still expected this year</div>
        <Line label="Interest" value={eur(bank.expected.interest_eur)} note={interestBasis(bank)} />
        <Line label="Dividends" value={eur(bank.expected.dividends_eur)} note="same months last year" />
        {vorab != null && vorab > 0 ? (
          <Line label="Vorabpauschale in January" value={eur(vorab)} />
        ) : null}
      </div>
    </div>
  );
}

/**
 * Freistellungsauftrag and NV certificate per bank: what each bank covers, what it
 * will withhold, and what to change. Estimate only; the banks' statements decide.
 */
export function AllowancePlanCard({ year }: { year: number }) {
  const query = useQuery({
    queryKey: ["tax", "allowances", year],
    queryFn: () => api<TaxAllowancePlan>(`/api/tax/allowances?year=${year}`),
  });
  const plan = query.data;

  if (!plan) {
    return (
      <div className="space-y-2 rounded-lg border border-border bg-surface p-4">
        <Header />
        <p className={cn("text-xs", query.isError ? "text-danger" : "text-text-secondary")}>
          {query.isError ? "Could not load the allowance plan." : "Working out the allowances…"}
        </p>
      </div>
    );
  }

  if (!plan.applicable) {
    return (
      <div className="space-y-2 rounded-lg border border-border bg-surface p-4">
        <Header />
        <p className="text-xs text-text-secondary">{plan.reason ?? "Not available for this tax year."}</p>
      </div>
    );
  }

  const labelOf = (bank: string | null) => plan.banks.find((b) => b.bank === bank)?.label ?? null;
  const saving =
    plan.projected_withheld_eur != null && plan.recommended_withheld_eur != null
      ? plan.projected_withheld_eur - plan.recommended_withheld_eur
      : 0;
  const unassigned = plan.unassigned?.events ?? 0;

  return (
    <div className="space-y-4 rounded-lg border border-border bg-surface p-4">
      <Header />

      {plan.actions.length > 0 ? (
        <ul className="space-y-2">
          {plan.actions.map((action, i) => (
            <ActionItem key={`${action.code}-${action.bank ?? i}`} action={action} bankLabel={labelOf(action.bank)} />
          ))}
        </ul>
      ) : (
        <p className="text-xs text-text-secondary">Nothing to change right now.</p>
      )}

      <BanksTable banks={plan.banks} />

      <div className="space-y-1 text-xs text-text-secondary">
        <p className="tabular-nums">
          Sparer-Pauschbetrag {eur(plan.allowance_eur)} · at other banks {eur(plan.other_banks_eur)} · on file{" "}
          <span className={plan.over_assigned ? "font-medium text-warn" : undefined}>{eur(plan.assigned_eur)}</span>
          {plan.over_assigned ? " (more than the allowance)" : ""}
        </p>
        {saving > 0 ? (
          <p>With the suggested split about {eur(saving)} less is withheld this year.</p>
        ) : null}
        {unassigned > 0 ? (
          <p>
            {unassigned} tax event{unassigned === 1 ? " has" : "s have"} no bank, so {unassigned === 1 ? "it counts" : "they count"}{" "}
            toward the yearly total but not toward any bank's Freistellungsauftrag.
          </p>
        ) : null}
      </div>

      <details className="text-xs text-text-secondary">
        <summary className="cursor-pointer font-medium hover:text-text-primary">What the projection includes</summary>
        <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
          {plan.banks.map((b) => (
            <BankBreakdown key={b.bank} bank={b} />
          ))}
        </div>
      </details>

      {plan.sources.length > 0 ? (
        <details className="text-xs text-text-secondary">
          <summary className="cursor-pointer font-medium hover:text-text-primary">Sources</summary>
          <ul className="mt-2 list-inside list-disc space-y-1">
            {plan.sources.map((s) => (
              <li key={s.url}>
                <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
                  {s.label}
                </a>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
