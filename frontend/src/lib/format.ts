// One formatter for the whole app. The UI is German: every number, percentage,
// currency and date renders with the fixed `de-DE` locale ("1.234,56 €",
// "12,3 %", "30.09.2026") regardless of the browser/OS language, so a phone set
// to English does not flip half the screens to dot decimals.
//
// Conventions
// - Percentages take a FRACTION (0.123 -> "12,3 %"). Values that are already in
//   percent units (12.3) go through `formatPercentPoints`.
// - null / undefined / NaN / ±Infinity render as an em dash.
// - `Intl.NumberFormat` instances are cached per option set (construction is
//   the expensive part and tables call these per cell).
// - `toFixed` stays in the codebase only where the output is NOT display text
//   (input values, keys, API payloads, chart-internal dot decimals).

export const APP_LOCALE = "de-DE";

const EMPTY = "—";
let reportingCurrency = "EUR";

// A caller-supplied locale is not guaranteed to be a valid BCP 47 tag (e.g.
// "en-US@posix" from a headless browser) and Intl throws RangeError on one,
// which used to crash whole pages. Anything invalid falls back to the app locale.
function resolveLocale(candidate?: string | null): string {
  if (!candidate) return APP_LOCALE;
  try {
    return Intl.getCanonicalLocales(candidate)[0] ?? APP_LOCALE;
  } catch {
    return APP_LOCALE;
  }
}

export function setReportingCurrency(currency: string | null | undefined) {
  if (currency) {
    reportingCurrency = currency;
  }
}

const numberFormats = new Map<string, Intl.NumberFormat>();

function numberFormat(locale: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${locale}|${JSON.stringify(options)}`;
  let format = numberFormats.get(key);
  if (!format) {
    try {
      format = new Intl.NumberFormat(locale, options);
    } catch {
      // Unknown currency code etc.: degrade to a plain decimal instead of crashing.
      format = new Intl.NumberFormat(APP_LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    numberFormats.set(key, format);
  }
  return format;
}

function isBlank(value: number | null | undefined): value is null | undefined {
  return value == null || typeof value !== "number" || !Number.isFinite(value);
}

export interface NumberOptions {
  /** Fraction digits (min = max unless `minDigits` is given). Default 2. */
  digits?: number;
  /** Alias of `digits`, kept so older call sites keep working. */
  decimals?: number;
  minDigits?: number;
  /** Always show the sign: "+1,2" / "-1,2" (zero stays unsigned). */
  signed?: boolean;
  locale?: string;
}

export function formatNumber(value: number | null | undefined, opts: NumberOptions = {}): string {
  if (isBlank(value)) return EMPTY;
  const max = opts.digits ?? opts.decimals ?? 2;
  const min = Math.min(opts.minDigits ?? max, max);
  return numberFormat(resolveLocale(opts.locale), {
    minimumFractionDigits: min,
    maximumFractionDigits: max,
    ...(opts.signed ? { signDisplay: "exceptZero" as const } : {}),
  }).format(value);
}

export interface PercentOptions {
  /** Fraction digits. Default 2. */
  digits?: number;
  /** Alias of `digits`, kept so older call sites keep working. */
  decimals?: number;
  /** Always show the sign: "+1,2 %" / "-1,2 %" (zero stays unsigned). */
  signed?: boolean;
  locale?: string;
}

/** `fraction` is a fraction: 0.123 -> "12,3 %". */
export function formatPercent(fraction: number | null | undefined, opts: PercentOptions = {}): string {
  if (isBlank(fraction)) return EMPTY;
  const digits = opts.digits ?? opts.decimals ?? 2;
  return numberFormat(resolveLocale(opts.locale), {
    style: "percent",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    ...(opts.signed ? { signDisplay: "exceptZero" as const } : {}),
  }).format(fraction);
}

/** For values already in percent units (12.3 -> "12,3 %"), e.g. `*_pct` API fields that are not fractions. */
export function formatPercentPoints(points: number | null | undefined, opts: PercentOptions = {}): string {
  if (isBlank(points)) return EMPTY;
  return formatPercent(points / 100, opts);
}

export interface CurrencyOptions {
  currency?: string;
  /** "1,2 Mio. €" style for tight spaces (KPI tiles, chart axes). */
  compact?: boolean;
  /** Always show the sign: "+12,00 €" / "-12,00 €" (zero stays unsigned). */
  signed?: boolean;
  /** Fraction digits; default follows the currency (2 for EUR) or 0-1 when compact. */
  digits?: number;
  locale?: string;
}

/**
 * `formatCurrency(1234.5)` -> "1.234,50 €". The currency defaults to the reporting
 * currency (EUR unless Control Center says otherwise) and may be passed
 * positionally or inside the options object: `formatCurrency(v, "USD")` and
 * `formatCurrency(v, { currency: "USD" })` are equivalent.
 */
export function formatCurrency(
  value: number | null | undefined,
  currencyOrOpts?: string | CurrencyOptions | null,
  maybeOpts: CurrencyOptions = {},
): string {
  if (isBlank(value)) return EMPTY;
  const opts: CurrencyOptions =
    typeof currencyOrOpts === "string"
      ? { ...maybeOpts, currency: currencyOrOpts }
      : { ...(currencyOrOpts ?? {}), ...maybeOpts };
  const options: Intl.NumberFormatOptions = {
    style: "currency",
    currency: opts.currency ?? reportingCurrency,
  };
  if (opts.compact) {
    options.notation = "compact";
    options.minimumFractionDigits = 0;
    options.maximumFractionDigits = opts.digits ?? 1;
  } else if (opts.digits != null) {
    options.minimumFractionDigits = opts.digits;
    options.maximumFractionDigits = opts.digits;
  }
  if (opts.signed) options.signDisplay = "exceptZero";
  return numberFormat(resolveLocale(opts.locale), options).format(value);
}

export function formatCompact(value: number | null | undefined): string {
  if (isBlank(value)) return EMPTY;
  return numberFormat(APP_LOCALE, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

// "2026-09-30" is a calendar date, not an instant: `new Date("2026-09-30")` is
// UTC midnight and renders as the 29th west of Greenwich. Build it in local time.
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value == null || value === "") return null;
  let date: Date;
  if (value instanceof Date) {
    date = value;
  } else if (typeof value === "string") {
    const match = DATE_ONLY.exec(value);
    date = match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : new Date(value);
  } else {
    date = new Date(value);
  }
  return Number.isNaN(date.getTime()) ? null : date;
}

const dateFormats = new Map<string, Intl.DateTimeFormat>();

function dateFormat(locale: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale}|${JSON.stringify(options)}`;
  let format = dateFormats.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat(locale, options);
    dateFormats.set(key, format);
  }
  return format;
}

export function formatDate(
  value: string | number | Date | null | undefined,
  opts: { style?: "short" | "medium" | "long"; locale?: string } = {},
): string {
  const date = toDate(value);
  if (!date) return EMPTY;
  return dateFormat(resolveLocale(opts.locale), { dateStyle: opts.style ?? "medium" }).format(date);
}

export function formatDateTime(
  value: string | number | Date | null | undefined,
  opts: { locale?: string } = {},
): string {
  const date = toDate(value);
  if (!date) return EMPTY;
  return dateFormat(resolveLocale(opts.locale), { dateStyle: "medium", timeStyle: "short" }).format(date);
}

const MONTH_ONLY = /^(\d{4})-(\d{2})$/;

/** "2026-10" (or any date) -> "Oktober 2026". Falls back to the raw string when it is not a month. */
export function formatMonth(value: string | Date | null | undefined, opts: { locale?: string } = {}): string {
  if (value == null || value === "") return EMPTY;
  const match = typeof value === "string" ? MONTH_ONLY.exec(value) : null;
  const date = match ? new Date(Number(match[1]), Number(match[2]) - 1, 1) : toDate(value);
  if (!date) return typeof value === "string" ? value : EMPTY;
  return dateFormat(resolveLocale(opts.locale), { month: "long", year: "numeric" }).format(date);
}

export interface SignedDelta {
  text: string;
  tone: "up" | "down" | "flat";
  /** Direction glyph so a delta never relies on colour alone. */
  arrow: "▲" | "▼" | "▬";
  /** Screen-reader text, e.g. "up +5,20 %". */
  label: string;
}

export function formatSignedDelta(
  value: number | null | undefined,
  kind: "currency" | "percent" | "number" = "number",
  opts: { currency?: string; digits?: number } = {},
): SignedDelta {
  if (isBlank(value)) return { text: EMPTY, tone: "flat", arrow: "▬", label: "no change data" };
  const tone = value > 0 ? "up" : value < 0 ? "down" : "flat";
  const arrow = value > 0 ? "▲" : value < 0 ? "▼" : "▬";
  const formatted =
    kind === "currency"
      ? formatCurrency(value, { currency: opts.currency, signed: true, digits: opts.digits })
      : kind === "percent"
        ? formatPercent(value, { signed: true, digits: opts.digits })
        : formatNumber(value, { signed: true, digits: opts.digits });
  // Intl leaves zero unsigned; keep the explicit "±0" marker for a flat delta.
  const text = tone === "flat" ? `±${formatted}` : formatted;
  const word = tone === "up" ? "up" : tone === "down" ? "down" : "unchanged";
  return { text, tone, arrow, label: `${word} ${text}` };
}
