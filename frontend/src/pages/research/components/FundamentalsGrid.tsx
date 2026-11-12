import { formatFundamental } from "../../../lib/strengthScore";

/** Fixed order and labels; keys not listed follow in the order the API sends them. */
const FUNDAMENTAL_FIELDS: { key: string; label: string }[] = [
  { key: "market_cap", label: "Market cap" },
  { key: "pe_ratio", label: "P/E (trailing)" },
  { key: "forward_pe", label: "P/E (forward)" },
  { key: "ev_ebitda", label: "EV / EBITDA" },
  { key: "pb_ratio", label: "Price / book" },
  { key: "fcf_yield", label: "Free-cash-flow yield" },
  { key: "dividend_yield", label: "Dividend yield" },
  { key: "gross_margin", label: "Gross margin" },
  { key: "profit_margin", label: "Profit margin" },
  { key: "roe", label: "Return on equity" },
  { key: "debt_equity", label: "Debt / equity" },
  { key: "net_debt_ebitda", label: "Net debt / EBITDA" },
  { key: "revenue_growth", label: "Revenue growth" },
  { key: "earnings_growth", label: "Earnings growth" },
  { key: "beta", label: "Beta" },
];
/** What each tile's number is made of, where the provider's definition differs from a company's own figure. */
const FIELD_HELP: Record<string, string> = {
  pe_ratio: "Price divided by the last twelve months of earnings per share. Lower means cheaper per euro of profit; negative earnings give no figure.",
  forward_pe: "Price divided by the analysts' expected earnings for the next twelve months.",
  pb_ratio: "Price divided by book value per share (assets minus debts). Below 1 means the market values the company under its book equity.",
  ev_ebitda: "Enterprise value (market cap plus net debt) divided by operating profit before depreciation. Compares companies with different debt.",
  beta: "How strongly the stock moved with its home index in the past. 1 moves with the market, above 1 moves more.",
  fcf_yield: "Provider free cash flow (operating cash flow less capex, before lease payments and minority interests) over market cap. A company's own adjusted FCF guidance gives a different, usually lower, yield.",
  net_debt_ebitda: "Provider total debt (leases included) minus cash, over unadjusted EBITDA. A company's lease-adjusted ratio is usually lower.",
  revenue_growth: "Growth over the period shown under the grid, in reported currency.",
  earnings_growth: "Growth over the period shown under the grid.",
};
/** Ratios that divide by book equity, greyed when the equity is too small for them to mean anything. */
const BOOK_RATIO_KEYS = new Set(["roe", "debt_equity"]);
/** Known extra keys get a label; any other key is hidden rather than shown as raw snake_case. */
const EXTRA_LABELS: Record<string, string> = {
  roic: "Return on invested capital",
  eps: "Earnings per share",
  average_volume: "Average volume",
  volume: "Volume",
  ter: "Annual cost (TER)",
  aum: "Fund assets",
};
const MONEY_KEYS = new Set(["market_cap", "aum"]);
const FCF_REASONS: Record<string, string> = {
  currency_mismatch: "n/a: cash flow and share price are in different currencies",
  financial_sector: "n/a: not meaningful for banks and insurers",
  out_of_range: "n/a: the provider's figure is implausible",
};

export function FundamentalsGrid({ fundamentals }: { fundamentals: Record<string, unknown> }) {
  const periodEnd = typeof fundamentals.latest_period_end === "string" ? fundamentals.latest_period_end : null;
  const months = typeof fundamentals.period_months === "number" ? fundamentals.period_months : null;
  const periodWord = months === 6 ? "half-year" : months === 3 ? "quarter" : months === 12 ? "year" : null;
  const unreliable = fundamentals.book_ratios_unreliable === true;
  const listed = new Set(FUNDAMENTAL_FIELDS.map((f) => f.key));
  const extra = Object.keys(EXTRA_LABELS)
    .filter((k) => !listed.has(k))
    .map((k) => ({ key: k, label: EXTRA_LABELS[k] }));
  const currency = typeof fundamentals.currency === "string" ? fundamentals.currency : null;
  const naReason = (key: string): string | null => {
    const r = fundamentals[`${key}_reason`];
    return fundamentals[key] == null && typeof r === "string" ? (FCF_REASONS[r] ?? "n/a") : null;
  };
  const cells = [...FUNDAMENTAL_FIELDS, ...extra].filter((f) => {
    const v = fundamentals[f.key];
    return (v != null && v !== "") || naReason(f.key) != null;
  });
  return (
    <>
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      {cells.map(({ key, label }) => {
        const greyed = unreliable && BOOK_RATIO_KEYS.has(key);
        return (
          <div
            key={key}
            data-testid={`fund-${key}`}
            data-greyed={greyed ? "true" : undefined}
            className={`rounded-md border border-border bg-surface-2 p-3 ${greyed ? "opacity-50" : ""}`}
            title={greyed ? "Book equity is too small for this ratio to mean anything (typically after years of buybacks)." : FIELD_HELP[key]}
          >
            <div className="text-[11px] uppercase tracking-wider text-text-muted">{label}</div>
            {naReason(key) ? (
              <div className="text-xs text-text-muted mt-1">{naReason(key)}</div>
            ) : (
              <div className="text-sm font-semibold mt-1">
                {formatFundamental(key, fundamentals[key])}
                {MONEY_KEYS.has(key) && currency ? ` ${currency}` : ""}
              </div>
            )}
            {greyed && <div className="text-[10px] text-text-muted mt-1">not meaningful: book equity too small</div>}
          </div>
        );
      })}
    </div>
    {periodEnd && (
      <p className="text-[11px] text-text-muted mt-2" data-testid="fund-period-note">
        Growth covers the {periodWord ? `${periodWord} to` : "reporting period ending"} {periodEnd}
        {periodWord ? ", against the same period a year earlier" : fundamentals.growth_basis === "quarter_yoy" ? "; companies that report twice a year show six months here" : ""}.
      </p>
    )}
    </>
  );
}
