/**
 * API client + typed backend-contract types.
 *
 * Orphan policy (audit 2026-08 §11 lane-14 / plan todo 34): exported types and
 * endpoint accessors with zero references outside this file are pruned.
 * Nothing is allowlisted any more (markNotificationRead, the last exception,
 * is used by the Decide page); unused exports must be deleted rather than
 * accumulated.
 */
const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";

function isAuthSessionPath(path: string): boolean {
  return path.startsWith("/api/auth/");
}

/** Only local absolute paths survive; "//host" and absolute URLs fall back to "/" (open-redirect guard). */
export function sanitizeNextPath(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//")) return "/";
  return path;
}

const LOGIN_REDIRECT_GUARD_KEY = "quantfolio:login-redirect-pending";

function clearLoginRedirectGuard(): void {
  try {
    window.sessionStorage.removeItem(LOGIN_REDIRECT_GUARD_KEY);
  } catch {
    // sessionStorage unavailable — nothing to clear
  }
}

function redirectToLogin(): void {
  const next = sanitizeNextPath(`${window.location.pathname}${window.location.search}`);
  try {
    window.sessionStorage.setItem(LOGIN_REDIRECT_GUARD_KEY, "1");
  } catch {
    // sessionStorage unavailable — proceed without the loop guard
  }
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

/** One-shot expired-session redirect: /login?next=<path>, sessionStorage-guarded against loops. */
function handleUnauthorized(path: string): void {
  if (isAuthSessionPath(path)) return;
  try {
    if (window.sessionStorage.getItem(LOGIN_REDIRECT_GUARD_KEY) === "1") return;
  } catch {
    // storage unavailable — still redirect once
  }
  redirectToLogin();
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const isFormData = init?.body instanceof FormData;
  const response = await fetch(`${baseUrl}${path}`, {
    credentials: "include",
    headers: {
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
      ...(init?.headers ?? {})
    },
    ...init
  });
  if (!response.ok) {
    if (response.status === 401) {
      handleUnauthorized(path);
    }
    const detail = await response.text();
    let parsed: Record<string, unknown> | null = null;
    try {
      parsed = JSON.parse(detail) as Record<string, unknown>;
    } catch {
      parsed = null;
    }
    throw new Error(apiErrorMessage(parsed, detail || response.statusText));
  }
  if (!isAuthSessionPath(path)) {
    clearLoginRedirectGuard();
  }
  if (response.status === 204 || response.status === 205) {
    return undefined as T;
  }
  const body = await response.text();
  if (!body.trim()) {
    return undefined as T;
  }
  return JSON.parse(body) as T;
}

/**
 * The backend wraps every HTTPException as `{"error": {"message": detail}}`
 * (main.py), and `detail` is sometimes a dict (`{message, debug}`,
 * `{errors: [...]}`). Pull a readable sentence out of either shape instead of
 * dumping JSON into a toast.
 */
export function apiErrorMessage(parsed: Record<string, unknown> | null, fallback: string): string {
  const readable = (value: unknown, depth = 0): string | undefined => {
    if (typeof value === "string") return value;
    if (!value || typeof value !== "object" || depth > 3) return undefined;
    const obj = value as Record<string, unknown>;
    const text = readable(obj.message, depth + 1) ?? readable(obj.detail, depth + 1) ?? readable(obj.error, depth + 1);
    if (text === undefined) return undefined;
    return typeof obj.debug === "string" ? `${text}\n${obj.debug}` : text;
  };
  const envelope = parsed?.error ?? parsed?.detail;
  if (envelope === undefined) return fallback;
  return readable(envelope) ?? JSON.stringify(envelope);
}

export type AuthSession = {
  user_id: string;
  username: string;
  generate_summary: boolean;
};

export type AuthMeResponse = {
  session: AuthSession;
};

export type SetupStatus = {
  initialized: boolean;
  has_passkey: boolean;
  integrations: Record<string, boolean>;
  next_step: string;
};

export type SettingsCatalogStatus = "active" | "needs_worker_restart" | "experimental";
export type SettingsInputType = "text" | "number" | "boolean" | "password" | "select" | "json" | "date" | "provider_chain";
export type SettingsUnit = "fraction_pct" | "pct" | "pp" | "eur" | "days" | "hours" | "seconds" | "points";

export type SettingsCatalogEntry = {
  key: string;
  /** The Control Center page the setting lives on (see SettingsPageMeta.group). */
  group: string;
  label: string;
  help: string;
  doc_url?: string | null;
  input_type: SettingsInputType;
  sensitive: boolean;
  status: SettingsCatalogStatus;
  options?: string[] | null;
  extended_help?: string | null;
  setup_steps?: string[] | null;
  section?: string | null;
  advanced?: boolean;
  unit?: SettingsUnit | null;
  option_labels?: Record<string, string> | null;
  /** Set when the setting is edited inside a connection drawer. */
  connection?: string | null;
  placeholder?: string | null;
  min?: number | null;
  max?: number | null;
  default?: unknown;
};

export type SettingsArea = "you" | "connections" | "engine" | "system";

export type SettingsPageMeta = {
  group: string;
  path: string;
  area: SettingsArea;
  label: string;
  description: string;
};

export type SettingsConnectionMetaField = {
  key: string;
  label: string;
  sensitive: boolean;
  placeholder?: string | null;
};

export type SettingsConnection = {
  id: string;
  group: string;
  label: string;
  description: string;
  secret: string | null;
  secret_label: string;
  secret_optional: boolean;
  meta_fields: SettingsConnectionMetaField[];
  enabled_key: string | null;
  testable: boolean;
  panel: string | null;
  /** Text settings that may stay empty without the connection counting as not set up. */
  optional_settings?: string[];
  settings: string[];
};

export type SettingsSchemaResponse = {
  catalog: Record<string, SettingsCatalogEntry>;
  pages: SettingsPageMeta[];
  connections: SettingsConnection[];
  completeness?: {
    public_setting_keys: string[];
    sensitive_integration_keys: string[];
  };
};

export type SettingsRecord = Record<string, any>;

export type ConnectionTestResult = {
  ok: boolean;
  message: string;
  tested_at: string;
};

export type SettingsIntegrationState = {
  configured: boolean;
  last_test?: ConnectionTestResult | null;
};

export type SettingsPayload = {
  settings: SettingsRecord;
  integrations: Record<string, SettingsIntegrationState>;
};

export const getSettingsSchema = () => api<SettingsSchemaResponse>("/api/settings/schema");

export type AttentionSeverity = "error" | "warning" | "info";

export type AttentionItem = {
  id: string;
  severity: AttentionSeverity;
  title: string;
  detail: string;
  href: string;
  action: string;
};

export type AttentionResponse = {
  items: AttentionItem[];
  pending_restart: string[];
};

export const getSettingsAttention = () => api<AttentionResponse>("/api/settings/attention");

export const getSettings = () => api<SettingsPayload>("/api/settings");
export const updateSettings = (payload: { settings?: SettingsRecord; integrations?: SettingsRecord }) =>
  api<SettingsPayload>("/api/settings", { method: "PUT", body: JSON.stringify(payload) });

export type ProviderChainEntry = {
  provider: string;
  enabled: boolean;
  available: boolean;
  message: string;
};

export type ProviderChainResponse = {
  providers: ProviderChainEntry[];
};

export const getProviderChain = () => api<ProviderChainResponse>("/api/settings/provider-chain");

export type IntegrationTestResponse = {
  service: string;
  ok: boolean;
  message: string;
  /** Set when the saved configuration was tested and the result recorded. */
  tested_at?: string | null;
};

export const testSettingsIntegration = (payload: { service: string; value?: string; meta?: SettingsRecord }) =>
  api<IntegrationTestResponse>("/api/settings/integrations/test", {
    method: "POST",
    body: JSON.stringify({ meta: {}, ...payload }),
  });

export interface TelegramLinkStatus {
  linked: boolean;
  linked_at: string | null;
}

export interface TelegramPairingCode {
  code: string;
  expires_at: string;
}

export async function getTelegramLinkStatus(): Promise<TelegramLinkStatus> {
  return api<TelegramLinkStatus>("/api/telegram/link-status");
}

export async function createTelegramPairingCode(): Promise<TelegramPairingCode> {
  return api<TelegramPairingCode>("/api/telegram/pairing-code", { method: "POST" });
}

export type Holding = {
  id: string;
  name: string;
  ticker?: string;
  isin?: string;
  asset_type: string;
  quantity: string;
  avg_buy_price: string | null;
  buy_date?: string;
  currency: string;
  source?: string;
  dkb_available: boolean;
};

export type DkbSync = {
  session_id: string;
  state: "pending_tan" | "waiting_for_push" | "needs_manual_tan" | "confirmed" | "failed" | "cached" | "expired";
  message: string;
  challenge?: string;
  challenge_html?: string;
  decoupled: boolean;
  available_tan_methods: { id: string; name: string }[];
  next_poll_after_seconds?: number;
  created_at?: string;
  updated_at?: string;
  provider?: string;
  logs?: { state: string; message: string; provider: string; created_at: string }[];
};

export type DkbProviderStatus = {
  provider: string;
  configured: boolean;
  product_id_required: boolean;
  product_id_configured: boolean;
  product_id_preview?: string | null;
  product_id_source?: string | null;
  message: string;
};

/** One row of the combined book per place it is held (GET /api/portfolio/wealth). */
export interface WealthBrokerRow {
  source: string;
  label: string;
  securities: number;
  cash: number;
  total: number;
  last_synced: string | null;
}

export type WealthSummary = {
  currency: string;
  total_value: number;
  by_broker?: WealthBrokerRow[];
  cash_value: number;
  security_value: number;
  manual_value: number;
  dkb_security_value: number;
  /** Securities at brokers synced besides DKB (Scalable Capital). */
  broker_security_value?: number;
  accounts: {
    id: string;
    source: string;
    name: string;
    institution?: string;
    type: string;
    iban?: string;
    balance: number;
    currency: string;
    last_synced?: string;
  }[];
  positions: {
    id: string;
    /** "dkb", "scalable", or the source of a hand-entered holding ("manual", a CSV import). */
    source: string;
    /** Broker name for synced positions, e.g. "Scalable Capital". */
    broker?: string;
    /** The synced depot holding it (two depots at one broker stay apart). */
    account_id?: string;
    isin?: string;
    symbol?: string;
    name: string;
    asset_type?: string;
    quantity: number;
    current_price?: number;
    current_value: number;
    currency?: string;
    last_synced?: string;
  }[];
  cashflow_30d: { income: number; outflow: number; net: number };
  last_snapshot?: string;
  generated_at: string;
};

export type PortfolioSnapshot = {
  date: string;
  total_value: number;
  cash_value: number;
  security_value: number;
  currency: string;
};

export type PortfolioActivity = {
  id: string;
  connected_account_id?: string;
  source: string;
  activity_type: string;
  date: string;
  amount: string | number;
  currency: string;
  description: string;
  isin?: string;
  symbol?: string;
  quantity?: string | number;
  price?: string | number;
  review_state: string;
};

export type PortfolioTransaction = {
  id: string;
  holding_id: string;
  type: string;
  date: string;
  quantity: string;
  price: string;
  fees: string;
  notes?: string;
};

export type WatchlistItem = {
  id: string;
  ticker?: string;
  isin?: string;
  name: string;
  notes?: string;
  added_date: string;
  horizon_tag: "short" | "mid" | "long";
  /** Decimal on the backend, serialised as a string (e.g. "150.000000"). */
  target_price?: number | string | null;
  alert_triggered?: boolean;
  alert_triggered_at?: string | null;
};

export type DiagnosticStep = {
  key: string;
  label: string;
  status: "passed" | "warning" | "failed";
  message: string;
};

export type DkbDiagnostic = {
  id: string;
  provider: string;
  status: "passed" | "warning" | "failed";
  summary: string;
  steps: DiagnosticStep[];
  created_at: string;
  debug_log?: string;
};

export type ProviderHealth = {
  id: string;
  provider: string;
  capability: string;
  configured: boolean;
  available: boolean;
  status: string;
  message: string;
  last_success_at?: string;
  last_error_at?: string;
  updated_at?: string;
};

export type Recommendation = {
  id: string;
  ticker?: string;
  verdict: string;
  confidence?: number;
  horizon?: "short" | "mid" | "long";
  mode?: "long_term" | "short_term";
  approval_state?: string;
  expires_at?: string;
  risk_score?: number;
  data_quality_score?: number;
  portfolio_fit_score?: number;
  summary?: string;
  created_at: string;
};

export type RecommendationDetail = Recommendation & {
  asset?: { symbol?: string; isin?: string; name: string; asset_type: string; currency: string };
  mode?: "long_term" | "short_term";
  approval_state?: string;
  evidence_score?: number;
  data_quality_score?: number;
  risk_score?: number;
  portfolio_fit_score?: number;
  expected_role?: string;
  suggested_position_size?: { min_pct: number; max_pct: number; reason: string };
  thesis?: string;
  reasoning?: string;
  bear_case?: string;
  bull_case?: string;
  pros?: string[];
  cons?: string[];
  why_not_buy?: string[];
  invalidation_triggers?: string[];
  required_human_checks?: string[];
  portfolio_fit?: string;
  risk_notes?: string[];
  sell_trigger?: string;
  backtest_summary?: Record<string, any>;
  benchmark_comparison?: Record<string, any>;
  macro_context?: string;
  backtest?: Record<string, unknown>;
};

export type RecommendationGenerateResult = {
  created: Recommendation[];
  failed: { ticker: string; message: string }[];
  message: string;
};

export type AnalysisReport = {
  id: string;
  title: string;
  ticker?: string;
  horizon: "short" | "mid" | "long";
  source: string;
  content: string;
  created_at: string;
};

export type NewsItem = {
  id: string;
  title: string;
  summary?: string;
  url?: string;
  source: string;
  ticker?: string;
  sentiment_label?: string;
  sentiment_score?: number | null;
  published_at: string;
  is_macro: boolean;
  relevance_score?: number | null;
  relevance_label?: string | null;
  relevance_reason?: string | null;
};

export type NewsRefreshResult = {
  created: number;
  symbols: string[];
  failed: string[];
  symbols_detail: Array<{
    symbol: string;
    status: "ok" | "skipped" | "failed";
    items: number;
    provider: string;
    message: string;
  }>;
};

export type RssFeedItem = {
  title: string;
  link: string;
  summary: string;
  published: string;
  source: string;
};

export type RssFeedResponse = {
  items: RssFeedItem[];
  sources: Record<string, number>;
  errors: string[];
  total: number;
};

export type MarketQuote = {
  ticker: string;
  price?: number;
  currency?: string;
  source: string;
  stale: boolean;
  message?: string;
};

export type MarketHistoryPoint = {
  date: string;
  close: number;
  stale: boolean;
};

export type Category = {
  id: string;
  name: string;
  color: string;
  icon: string;
  type: "income" | "expense" | "investment";
};

// --- Regime engine ------------------------------------------------------

export type RegimeLabel = "bull" | "bear" | "sideways";

export type RegimeCurrent = {
  ts: string | null;
  label: RegimeLabel | null;
  score: number | null;
  crisis: boolean;
  probs: Record<string, number>;
  source: string | null;
  stale?: boolean | null;
  age_hours?: number | null;
};

// Macro regime snapshot (Phase 1 — VIX + yield curve + momentum)
export type MacroRegimeLabel = "bull" | "sideways" | "bear" | "unknown";

/** The statistical jump model's latest state (lab/regime/macro_snapshot.py). */
export type MacroRegimeSnapshot = {
  label: MacroRegimeLabel | string;
  /** Always null: a state, not a probability. */
  confidence: number | null;
  model: string;
  available: boolean;
  reason?: string | null;
  crisis?: boolean;
  vix: number | null;
  credit_spread: number | null;
  drawdown: number | null;
  state_since: string | null;
  as_of: string | null;
  stale?: boolean;
  updated_at: string;
};

export const getMacroRegime = () => api<MacroRegimeSnapshot>("/api/quant/regime/current");

// --- V2: Tax cockpit ----------------------------------------------------

export type TaxSettings = {
  tax_residency_country?: string;
  church_tax?: string;
  freistellungsauftrag_amount?: number;
  freistellungsauftrag_used?: number;
  tax_spouse_allowance?: boolean;
  tax_estimation_enabled?: boolean;
  tax_nv_certificate?: boolean;
  tax_nv_valid_until?: string | null;
  /** Freistellungsauftrag on file per bank (EUR; null = not entered). */
  freistellungsauftrag_dkb_eur?: number | null;
  freistellungsauftrag_scalable_eur?: number | null;
  /** Total of the orders on file at other banks (EUR; null = none entered). */
  freistellungsauftrag_other_banks_eur?: number | null;
  /** DKB Tagesgeld interest rate as a fraction. */
  tax_interest_rate_dkb?: number | null;
  /** Which banks hold a copy of the NV certificate. */
  tax_nv_filed_dkb?: boolean | null;
  tax_nv_filed_scalable?: boolean | null;
  tax_other_income_eur?: number;
  tax_health_insurance?: "unknown" | "de_family" | "de_own" | "foreign";
  tax_bafoeg?: boolean;
};

export type GainHarvestStep = {
  isin: string;
  name?: string | null;
  sell_quantity: number;
  notional_eur: number;
  gain_eur: number;
  taxable_gain_eur: number;
  trading_cost_eur: number;
  future_tax_avoided_eur: number;
  net_benefit_eur: number;
  whole_position: boolean;
  /** The bank that holds the position ("dkb", "scalable"). */
  bank?: string | null;
};

export type GainHarvestBankRoom = {
  bank: string;
  label: string;
  nv_covers: boolean;
  /** What the bank's Freistellungsauftrag still covers; null when no per-bank cap applies. */
  room_eur: number | null;
};

export type GainHarvest = {
  tax_year: number;
  estimate: true;
  not_tax_advice: true;
  enabled: boolean;
  reason?: string | null;
  /** "nv": room under the Grundfreibetrag; "allowance": what each bank's Freistellungsauftrag still covers. */
  mode?: "nv" | "allowance";
  nv_certificate?: { valid_until: string | null; expires_in_days: number | null } | null;
  health_insurance?: string;
  room?: {
    mode?: "nv" | "allowance";
    sparer_pauschbetrag_eur: number;
    capital_income_so_far_eur: number;
    room_eur: number;
    /** NV mode only. */
    grundfreibetrag_eur?: number;
    other_income_eur?: number;
    tax_free_room_eur?: number;
    family_insurance_room_eur?: number | null;
    /** Allowance mode only. */
    projected_capital_income_eur?: number;
  other_banks_income_eur?: number | null;
  };
  bank_rooms?: GainHarvestBankRoom[];
  steps: GainHarvestStep[];
  totals?: { taxable_gain_eur: number; trading_cost_eur: number; future_tax_avoided_eur: number };
  skipped: Array<{ isin: string; name?: string | null; reason: string }>;
  warnings: Array<{ code: string; message: string }>;
};

export interface TaxAllowanceAction {
  code: string;
  severity: "action" | "warning" | "info";
  title: string;
  message: string;
  bank: string | null;
  /** ISO date. */
  deadline: string | null;
  how_to: string | null;
}

export interface TaxAllowanceBank {
  bank: "dkb" | "scalable" | string;
  label: string;
  fsa_eur: number | null;
  nv_filed: boolean;
  nv_covers: boolean;
  booked_eur: number;
  used_eur: number;
  remaining_eur: number;
  projected_eur: number;
  projected_uncovered_eur: number;
  projected_withheld_eur: number;
  minimum_fsa_eur: number;
  recommended_fsa_eur: number;
  recommended_withheld_eur: number;
  change_eur: number;
  booked: {
    dividends_eur: number | null;
    interest_eur: number | null;
    vorabpauschale_eur: number | null;
    gains_eur: number | null;
    losses_eur: number | null;
  };
  expected: {
    interest_eur: number | null;
    interest_basis: "rate" | "run_rate" | "unknown" | "none";
    dividends_eur: number | null;
    vorabpauschale_eur: number | null;
  };
  vorabpauschale_estimated_eur: number | null;
  savings_balance_eur: number | null;
  /** Fraction. */
  interest_rate: number | null;
  loss_left: { aktien_eur: number | null; other_eur: number | null };
  harvestable_gain_eur: number | null;
  how_to: { fsa?: string; nv?: string; loss?: string };
}

export interface TaxAllowancePlan {
  tax_year: number;
  label?: string;
  estimate: true;
  not_tax_advice: true;
  applicable: boolean;
  reason: string | null;
  allowance_eur?: number;
  other_banks_eur?: number;
  available_eur?: number;
  assigned_eur?: number;
  over_assigned?: boolean;
  withholding_rate?: number;
  projected_withheld_eur?: number;
  recommended_withheld_eur?: number;
  changes_needed?: boolean;
  projected_capital_income_eur?: number;
  unassigned?: { events: number; taxable_eur: number | null; vorabpauschale_eur: number | null };
  nv?: {
    has_certificate: boolean;
    valid_for_year: boolean;
    valid_until: string | null;
    ends_this_year: boolean;
    filed_at: string[];
    banks_known: boolean;
    ends_midyear?: boolean;
    filed: Record<string, boolean>;
    eligibility: {
      limit_eur: number | null;
      income_eur: number | null;
      headroom_eur: number | null;
      likely_eligible: boolean;
      grundfreibetrag_assumed: boolean;
    };
  };
  banks: TaxAllowanceBank[];
  actions: TaxAllowanceAction[];
  sources: { label: string; url: string }[];
}

export type TaxOverview = {
  tax_year: number;
  label: string;
  estimate: true;
  not_tax_advice: true;
  allowance: {
    amount_eur: number;
    used_before_eur: number;
    used_now_eur: number;
    remaining_after_eur: number;
  };
  lines: Record<string, number>;
  tax: Record<string, number>;
  carryforward: { aktien_loss_eur: number; sonstige_loss_eur: number };
  anlage_kap_mapping: Record<string, any>;
  vorabpauschale_estimate?: {
    estimated_from_holdings: boolean;
    taxable_eur: number;
    items: Array<{
      isin?: string | null;
      ticker?: string | null;
      name?: string | null;
      fund_class: string;
      teilfreistellung_pct: number;
      fund_value_start_eur: number;
      fund_value_end_eur: number;
      basiszins: number;
      basiszins_known: boolean;
      months_held: number;
      vorabpauschale_eur: number;
      taxable_after_teilfreistellung_eur: number;
    }>;
  };
  provenance: { events_by_source: Record<string, number>; events_total: number };
  /** Withheld by the banks vs. owed after NV and Günstigerprüfung (DE only). */
  position?: TaxPosition;
};

export interface TaxPosition {
  estimate: true;
  not_tax_advice: true;
  tax_year: number;
  applicable: boolean;
  reason?: string | null;
  projected_capital_income_eur?: number;
  other_income_eur?: number;
  withheld_by_banks_eur?: number;
  flat_rate_tax_eur?: number;
  tariff_tax_eur?: number;
  tariff_assumed?: boolean;
  use_guenstigerpruefung?: boolean;
  final_tax_eur?: number;
  refund_with_return_eur?: number;
  owed_with_return_eur?: number;
  nv_valid_for_year?: boolean;
  headline?: string;
  notes?: string[];
}

export interface RecentRun {
  id: string;
  type: "experiment" | "backtest";
  title: string;
  subtitle: string;
  status: string;
  metric_label: string;
  metric_value: number | null;
  created_at: string | null;
  ref: string;
}

export type TaxEvent = {
  id: string;
  tax_year: number;
  event_date: string;
  event_type: "dividend" | "interest" | "sale" | "vorabpauschale" | "withholding" | "fee";
  isin?: string | null;
  symbol?: string | null;
  name?: string | null;
  fund_class?: string | null;
  teilfreistellung_pct: number;
  gross_eur: number;
  withheld_eur: number;
  foreign_wht_eur: number;
  foreign_country?: string | null;
  realised_gain_eur?: number | null;
  bucket?: string | null;
  confidence: string;
  source: string;
  source_ref?: string | null;
  /** The bank that booked it ("dkb", "scalable", …); null when unknown. */
  institution?: string | null;
  notes?: string | null;
};

export type TaxLot = {
  id: string;
  isin: string;
  symbol?: string | null;
  name?: string | null;
  fund_class: string;
  teilfreistellung_pct: number;
  acquired_at: string;
  quantity_initial: number;
  quantity_remaining: number;
  cost_basis_eur: number;
  fees_eur: number;
  source: string;
  source_ref?: string | null;
  closed_at?: string | null;
  confidence: string;
};

// --- V2: Quant portfolio analytics --------------------------------------

/**
 * Max-drawdown sub-object as returned by `quant_metrics.max_drawdown()`
 * (backend/app/services/quant_metrics.py). Always present with finite
 * values — that function returns `0.0` rather than `null`/`NaN` on empty
 * or degenerate input, so neither field is optional or nullable here.
 */
export interface QuantDrawdown {
  max_drawdown: number;
  max_drawdown_duration: number;
}

/**
 * Portfolio-level risk metrics as serialized by
 * `quant_metrics.full_risk_report()` (backend/app/services/quant_metrics.py).
 * Every scalar below is guaranteed present and finite by that function's
 * `_finite_or_zero()` guard, so none are optional/`undefined` — deliberately
 * NOT `number | null` for these, unlike the original unified-portfolio-engine
 * plan's assumption (docs/archive/plans/unified-portfolio-engine-implementation.md
 * Phase 8): the backend never omits or nulls them, it substitutes 0.0.
 *
 * `beta`/`alpha`/`r_squared`/`treynor` are the one genuine exception — they
 * are only added to the dict when a benchmark return series was supplied
 * (`app/api/quant/portfolio.py`), so they are truly absent (not null) from
 * the wire payload otherwise and stay optional.
 *
 * The plan's `cvar_95` corresponds to `historical_cvar` here (computed at
 * whatever `confidence` the caller passed — 0.95 by default); the plan's
 * `volatility` is `annualised_volatility`; the plan's `max_drawdown` is
 * nested under `drawdown.max_drawdown`, not flat. The plan's
 * `expected_return: {value, method, horizon}` does not exist on this shape
 * at all — that's `ReturnAnchor`/`select_return_anchor` from Phase 3
 * (backend/app/services/expected_return.py), which is only ever collapsed
 * to a plain `number | null` (`DiscoverCandidate.expected_return` /
 * `DiscoverDossier.expected_return` below) before it reaches the wire —
 * dossiers additionally carry the optional `expected_return_band`
 * (P10/P50/P90, M5 Option 0); no portfolio quant-metrics endpoint returns
 * the `{value, method, horizon}` object shape.
 */
export interface QuantRiskMetrics {
  samples?: number;
  annualised_return: number;
  annualised_volatility: number;
  downside_deviation: number;
  sharpe: number;
  sortino: number;
  calmar: number;
  skewness: number;
  kurtosis_excess: number;
  historical_cvar: number;
  parametric_cvar: number;
  drawdown: QuantDrawdown;
  beta?: number | null;
  /** Jensen's alpha: annualised intercept of the daily excess-return regression. */
  alpha?: number | null;
  r_squared?: number | null;
  treynor?: number | null;
  /** Newey-West t-statistic of alpha. */
  alpha_t_stat?: number | null;
  /** Lead/lag-summed beta (Dimson), robust to markets closing at different times. */
  beta_dimson?: number | null;
  tracking_error?: number | null;
  information_ratio?: number | null;
  /** Days both the book and the benchmark have a return. */
  benchmark_samples?: number | null;
}

/** Chart-ready series appended to `QuantRiskMetrics` by `quant_metrics.risk_series()`. */
export interface QuantRiskSeries {
  equity_curve: Array<{ date: string; value: number }>;
  rolling_drawdown: Array<{ date: string; value: number }>;
  returns: number[];
  regression_points: [number, number][];
  rolling_beta: Array<{ date: string; value: number }>;
}

export type QuantPortfolioSummary = {
  available: boolean;
  samples?: number;
  weights?: Record<string, number>;
  risk?: QuantRiskMetrics & QuantRiskSeries;
  diagnostics: Record<string, any>;
  message?: string;
};

export type QuantPortfolioRisk = {
  available: boolean;
  benchmark?: string | null;
  risk?: QuantRiskMetrics &
    QuantRiskSeries & {
      historical_var: number;
      parametric_var: number;
      insufficient_history: boolean;
      /** The annual risk-free rate used (ECB deposit rate unless overridden). */
      risk_free?: number;
    };
  diagnostics?: Record<string, any>;
};

/** GET /api/quant/portfolio/projection: Monte Carlo of the book in real EUR (wealth_planner.py). */
export interface PortfolioProjection {
  inputs: {
    start_value_eur: number;
    monthly_contribution_eur: number;
    years: number;
    real_return: number;
    volatility: number;
    nu: number;
    calibration_years: number;
    drift_standard_error: number;
    paths: number;
    seed: number;
    goal_eur: number | null;
  };
  fan: Array<{ year: number; p5: number; p25: number; p50: number; p75: number; p95: number; contributed: number }>;
  terminal: { median: number; mean: number; p5: number; p95: number; contributed: number };
  goal?: {
    goal_eur: number;
    probability: number;
    mc_standard_error: number;
    probability_low_drift: number;
    probability_high_drift: number;
    median_reaches_in_years: number | null;
  };
  assumptions: {
    real_return_source: string;
    real_return_as_of: string;
    real_return_overridden: boolean;
    volatility_source: string;
    fat_tails: string;
  };
  real_terms: true;
  estimate: true;
}

/** GET /api/quant/portfolio/real/performance: the book's time-weighted unit value. */
export interface BookPerformance {
  available: boolean;
  reason?: string;
  currency: "EUR";
  estimate: true;
  start?: string;
  end?: string;
  days?: number;
  benchmark?: string;
  series: Array<{ date: string; unit_value: number; value: number; net_flow: number; benchmark: number | null }>;
  value_start_eur?: number;
  value_end_eur?: number;
  net_contributions_eur?: number;
  /** Time-weighted return over the whole period (not annualised). */
  twr?: number;
  /** Only over a year or more (GIPS). */
  twr_annualised?: number | null;
  benchmark_return?: number | null;
  benchmark_annualised?: number | null;
  irr_annualised?: number | null;
  irr_period?: number | null;
  unpriced?: string[];
  days_without_full_prices?: number;
  method?: string;
}

// --- V2: Real (DKB) holdings analytics ------------------------------------

export interface RealHoldingsPosition {
  isin: string | null;
  ticker: string | null;
  name: string | null;
  quantity: number;
  current_price: number | null;
  current_value: number;
  weight: number | null;
  avg_buy_price: number | null;
  /** avg_buy_price × quantity; null when the broker knows no cost. */
  cost_basis?: number | null;
  unrealized_pnl: number | null;
  /** "dkb" or "scalable". */
  source?: string;
  /** "DKB" or "Scalable Capital". */
  broker?: string;
  account_id?: string;
}

/** One depot's share of a `by_ticker` row. */
export interface RealHoldingsDepot {
  source: string;
  broker: string;
  account_id: string;
  quantity: number;
  current_value: number;
}

/**
 * The `by_ticker` aggregate is a *narrower* object than `positions` — the
 * backend (`get_real_holdings_summary` in backend/app/foundation/portfolio/
 * bridge.py) builds it without `current_price`, `weight` and
 * `avg_buy_price`. Typing it as a full `RealHoldingsPosition` declared
 * those three as merely nullable, i.e. safe to read, when they are absent.
 * Despite the name it is keyed by ISIN, so one fund held at two brokers is
 * one row; `depots` lists where it sits.
 */
export interface RealHoldingsTickerAggregate {
  isin: string | null;
  ticker: string | null;
  name: string | null;
  quantity: number;
  current_value: number;
  unrealized_pnl: number | null;
  depots?: RealHoldingsDepot[];
}

/** One broker's depots summed. P&L counts only positions with a known cost. */
export interface RealHoldingsBrokerTotal {
  broker: string;
  total_value: number;
  weight: number;
  position_count: number;
  cost_basis: number;
  /** Market value of the positions the P&L covers. */
  costed_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number | null;
}

export interface RealHoldingsSummary {
  total_value: number;
  positions: RealHoldingsPosition[];
  by_ticker: Record<string, RealHoldingsTickerAggregate>;
  /** Keyed by source ("dkb", "scalable"). */
  by_broker?: Record<string, RealHoldingsBrokerTotal>;
  position_count: number;
  positions_with_ticker: number;
  isin_only_count: number;
}

/**
 * `metrics` omits the nested `drawdown` key — the wrapper hoists it to the
 * sibling `max_drawdown` field instead (`compute_real_holdings_metrics` in
 * backend/app/services/portfolio/metrics_wrappers.py).
 *
 * Both `metrics` and `max_drawdown` are only populated on the success path.
 * That wrapper has three `available: false` early returns — no price data, no
 * holdings with value, no ticker matched the price matrix — and each emits
 * `metrics: {}` with no `max_drawdown` key at all. A user with no DKB
 * positions, or with only unresolved ISINs, hits them, so consumers must
 * narrow on `available` (or use `?.`) before reading either.
 */
export interface RealPortfolioMetricsResponse {
  available: boolean;
  metrics: Partial<Omit<QuantRiskMetrics, "drawdown">>;
  max_drawdown?: QuantDrawdown;
  equity_curve: Array<{ date: string; value: number }>;
  diagnostics: Record<string, any>;
}

/** Same `available: false` caveat as `RealPortfolioMetricsResponse`. */
export interface RealPortfolioRiskResponse {
  available: boolean;
  metrics: Partial<Omit<QuantRiskMetrics, "drawdown">>;
  max_drawdown?: QuantDrawdown;
  factor_exposures: Record<string, number>;
  factor_r_squared: number;
  diagnostics: { metrics_diagnostics: Record<string, any>; factor_diagnostics: Record<string, any> };
}

export interface RealRebalanceSuggestion {
  ticker: string;
  action: "buy" | "sell";
  current_weight: number;
  target_weight: number;
  diff: number;
  estimated_amount: number;
}

/** One line of the band rebalance (foundation/portfolio/metrics_wrappers.py). */
export interface RebalanceLine {
  key: string;
  isin: string | null;
  ticker: string | null;
  name: string | null;
  value_eur: number;
  current_weight: number;
  target_weight: number;
  drift_pp: number;
  in_band: boolean;
  buy_eur: number;
  sell_eur: number;
  after_weight: number | null;
  /** Gain realised by the sale after Teilfreistellung; null without a cost basis. */
  taxable_gain_eur: number | null;
  /** Flat-rate tax on it before any allowance or NV certificate. */
  tax_eur: number | null;
}

export interface RealRebalanceResponse {
  available: boolean;
  total_value?: number;
  target_method?: string;
  target_label?: string;
  methods?: Record<string, string>;
  contribution_eur?: number;
  band_pp?: number;
  current_weights: Record<string, number>;
  optimal_weights: Record<string, number>;
  lines?: RebalanceLine[];
  suggestions: RealRebalanceSuggestion[];
  sells?: boolean;
  tax_eur?: number;
  tax_rate?: number;
  unpriced?: string[];
  optimization_status?: string | null;
  optimization_method?: string | null;
  diagnostics: Record<string, any>;
}

/** One covariance-only candidate allocation (foundation/allocation.py). */
export interface AllocationMethod {
  label: string;
  weights: Record<string, number>;
  volatility: number;
  risk_contributions: Record<string, number>;
  diversification_ratio: number | null;
  effective_n: number | null;
  turnover?: number;
}

export type QuantOptimisation = {
  available: boolean;
  reason?: string;
  assets?: string[];
  /** Fractions of the priced book. */
  weights_current?: Record<string, number>;
  values_current_eur?: Record<string, number>;
  methods?: Record<string, AllocationMethod>;
  current?: (Omit<AllocationMethod, "label" | "turnover"> & { weights: Record<string, number> }) | null;
  errors?: Record<string, string>;
  max_weight?: number | null;
  covariance?: {
    estimator: string;
    shrinkage: number;
    samples: number;
    start: string;
    end: string;
    annualised_volatility: Record<string, number>;
  };
  optimizations?: { methods?: Record<string, AllocationMethod> };
  diagnostics?: Record<string, any>;
};

export type QuantFanData = {
  steps: number;
  bands: Array<{ p05: number; p25: number; median: number; p75: number; p95: number }>;
  p05: number[];
  p25: number[];
  median: number[];
  p75: number[];
  p95: number[];
};

// --- Portfolio -> Risk (GET /api/verification/risk|stress/{id|main}) ---

export interface RiskAlertItem {
  id: string;
  type: string;
  severity: "info" | "warning" | "critical" | string;
  title: string;
  message: string;
  created_at: string;
}

/** What the VaR / drawdown / Sharpe numbers rest on (daily returns of the current holdings). */
export interface ReturnBasis {
  source: string;
  n_obs: number;
  start: string | null;
  end: string | null;
  priced_assets: string[];
  missing_history: string[];
  message: string | null;
}

export interface RiskAssessment {
  var_95: number;
  var_99: number;
  max_drawdown: number;
  current_drawdown: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  concentration_index: number;
  lookthrough_hhi: number;
  lookthrough_count: number;
  asset_type_exposure: Record<string, number>;
  currency_exposure: Record<string, number>;
  alerts: RiskAlertItem[];
  insufficient_history: boolean;
  return_basis: ReturnBasis;
}

export interface StressScenarioItem {
  scenario_name: string;
  description: string;
  portfolio_impact_pct: number;
  worst_case_loss: number;
  details: Record<string, number>;
}

export interface VerificationStressResult {
  portfolio_id: string;
  scenarios: StressScenarioItem[];
  note: string;
  created_at: string;
}

/** GET /api/performance/ledger/{id}/benchmark (inside the `research` envelope). */
export interface LedgerBenchmark {
  composite_id: string;
  benchmark_symbol: string;
  period_start: string;
  period_end: string;
  period_days: number;
  portfolio_twr: number | null;
  benchmark_return: number | null;
  excess: number | null;
  short_window: boolean;
  message: string | null;
}

// --- Can I trust it? (GET /api/trust/verdict | /calls) ---

export type TrustState = "too_early" | "skill" | "harm" | "no_evidence";
export type TrustTypeKey = "ideas" | "advisor" | "mandates" | "regime";

export interface TrustSkill {
  metric: string;
  value: number;
  ci_low: number | null;
  ci_high: number | null;
  /** Rebalance dates (the independent unit). */
  n: number;
  n_calls?: number;
  benchmark_label: string;
}

export interface TrustRangeCoverage {
  k: number;
  n: number;
  rate: number | null;
  ci_low: number | null;
  ci_high: number | null;
  nominal: number;
}

export interface TrustReliabilityBin {
  p_mean: number;
  hit_rate: number;
  n: number;
}

export interface TrustTypeVerdict {
  type: TrustTypeKey;
  label: string;
  /** Rebalance dates: the picks of one date form one equal-weight basket. */
  n: number;
  n_needed: number | null;
  n_issue_days: number;
  n_calls?: number;
  n_delisted?: number;
  call_hits?: number;
  call_hit_rate?: number | null;
  hits: number;
  hit_rate: number | null;
  hit_ci: [number, number] | null;
  mean_excess: number | null;
  mean_excess_ci: [number, number] | null;
  hac_lag?: number;
  /** Current e-values; the state comes from e-BH across every type and direction. */
  e_skill: number;
  e_harm: number;
  e_skill_peak?: number;
  e_harm_peak?: number;
  n_tests?: number;
  e_skill_crossed_strong: boolean;
  state: TrustState;
  benchmarked: boolean;
  benchmark_label: string;
  brier: number | null;
  bss: number | null;
  bss_ci: [number, number] | null;
  n_stated_p: number;
  spiegelhalter_z: number | null;
  reliability: TrustReliabilityBin[];
  range_coverage: TrustRangeCoverage | null;
  calibration_caption: string | null;
  next_resolution_at: string | null;
  note: string | null;
}

export interface TrustVerdict {
  headline: string;
  resolved_calls: number;
  /** Rebalance dates of the benchmarked types: the "n" of "n of ~600". */
  resolved_units?: number;
  n_needed?: number | null;
  target_hit_rate?: number;
  n_tests?: number;
  fdr_level?: number;
  skill: TrustSkill | null;
  state: TrustState;
  types: TrustTypeVerdict[];
  frozen_at_issue: boolean;
  method_note: string;
}

export interface TrustCall {
  type: TrustTypeKey;
  id: string;
  issued_at: string;
  resolved_at: string;
  subject: string;
  call: string;
  stated_p: number | null;
  stated_range: [number, number] | null;
  outcome: number | null;
  benchmark_outcome: number | null;
  excess: number | null;
  hit: boolean;
  verdict: string | null;
  delisted?: boolean;
}

export interface TrustCalls {
  type: TrustTypeKey | null;
  total: number;
  limit: number;
  offset: number;
  items: TrustCall[];
  worst_misses: TrustCall[];
  frozen_at_issue: boolean;
}

export type ChatConversation = {
  conversation_id: string;
  title: string;
  last_at: string;
  message_count: number;
};

export type ChatMessageItem = {
  id: string;
  conversation_id?: string | null;
  role: string;
  content: string;
  timestamp: string;
};

export interface QuantSavedScenario {
  id: string;
  name: string;
  annual_return: number;
  annual_volatility: number;
  years: number;
  simulations: number;
  start_value: number;
  p05_terminal: number;
  median_terminal: number;
  p95_terminal: number;
  fan_data: QuantFanData;
  notes?: string;
  created_at: string;
}

export const listSavedScenarios = () => api<QuantSavedScenario[]>("/api/quant/portfolio/scenarios/saved");

export interface LegacyScenarioParams {
  type: "legacy";
  name: string;
  annual_return: number;
  annual_volatility: number;
  years: number;
  simulations: number;
  start_value: number;
  notes?: string;
}

export interface StochasticScenarioParams {
  type: "stochastic";
  name: string;
  S0: number;
  mu: number;
  sigma: number;
  T: number;
  horizon_steps: number;
  n_paths: number;
  strike: number;
  nu: number;
  notes?: string;
}

/**
 * The save endpoint (SaveScenarioRequest) only knows the GBM fan fields; a
 * stochastic run's S0/mu/sigma/T/n_paths were ignored and the scenario was
 * saved with the defaults. Map them onto the fields it reads, within its bounds.
 */
export function toSaveScenarioBody(body: LegacyScenarioParams | StochasticScenarioParams) {
  if (body.type === "legacy") return body;
  return {
    name: body.name,
    notes: body.notes,
    start_value: body.S0,
    annual_return: body.mu,
    annual_volatility: body.sigma,
    years: Math.min(50, Math.max(1, Math.round(body.T))),
    simulations: Math.min(10_000, Math.max(100, body.n_paths)),
  };
}

export const saveScenario = (body: LegacyScenarioParams | StochasticScenarioParams) =>
  api<QuantSavedScenario>("/api/quant/portfolio/scenarios/saved", { method: "POST", body: JSON.stringify(toSaveScenarioBody(body)) });
export const deleteSavedScenario = (id: string) => api<{ deleted: string }>(`/api/quant/portfolio/scenarios/saved/${id}`, { method: "DELETE" });

// Data backbone types
export interface DataCoverage {
  symbol: string;
  first_ts: string | null;
  last_ts: string | null;
  bar_count: number | null;
  last_provider: string | null;
  gaps_count: number;
}

export const getDataCoverage = (symbol: string) => api<DataCoverage>(`/api/data/coverage?symbol=${symbol}`);
// Freshness of the research extracts in the Parquet panel (insider, IBES,
// JKP, WRDS links). The WRDS ones are exported by hand.
export interface ResearchExtract {
  key: string;
  label: string;
  refresh: string;
  stale_after_days: number;
  used_for: string;
  newest: string | null;
  rows: number;
  age_days: number | null;
  stale: boolean;
  error: string | null;
}

export const getResearchExtracts = () => api<ResearchExtract[]>("/api/data/research-extracts");

export const listSymbols = () => api<string[]>("/api/data/symbols");
export interface BackfillJob {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  days?: number;
  total?: number | null;
  succeeded?: number | null;
  failed?: number | null;
  error_message?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

// Enqueue an async holdings backfill (proposal P4). Returns a job handle to poll.
export const enqueueBackfillHoldings = (days = 1825) =>
  api<BackfillJob>(`/api/data/backfill/holdings/async?days=${days}`, { method: "POST" });

export const getBackfillJob = (jobId: string) =>
  api<BackfillJob>(`/api/data/backfill/${jobId}`);

// Monte Carlo types (Phase 2)
export interface McResult {
  research: {
    estimate: number;
    stderr: number;
    n_effective: number;
    variance_reduction_gain?: Record<string, number>;
    greeks?: {
      delta: number;
      delta_stderr: number;
      gamma: number;
      gamma_stderr: number;
      vega: number;
      vega_stderr: number;
      theta?: number;
      rho?: number;
    };
    percentiles?: {
      p05: number;
      p25: number;
      p50: number;
      p75: number;
      p95: number;
    };
    paths_summary?: {
      n_paths: number;
      n_steps: number;
      mean: number;
      std: number;
      min: number;
      max: number;
      percentiles: Record<string, number>;
      envelope: {
        p5: number[];
        p50: number[];
        p95: number[];
      };
    };
    mlmc_stats?: {
      n_levels: number;
      n_total_samples: number;
      level_stats: Array<{
        level: number;
        n_samples: number;
        estimate: number;
        variance: number;
        std_error: number;
      }>;
    };
  };
  attribution?: {
    brinson?: {
      allocation_effect: number;
      selection_effect: number;
      interaction_effect: number;
      total_active_return: number;
      securities?: Array<{
        isin: string;
        ticker: string;
        name: string;
        allocation_effect: number;
        selection_effect: number;
        interaction_effect: number;
        total_effect: number;
      }>;
    };
    factor_contribution?: {
      factors: Array<{ name: string; exposure: number; contribution: number }>;
      residual: number;
      r_squared: number;
    };
    twr?: number;
    mwr?: number;
  } | null;
  ledger_ref?: string | null;
}

// Phase 3 Attribution types
export interface AttributionResult {
  brinson?: {
    allocation_effect: number;
    selection_effect: number;
    interaction_effect: number;
    securities: Array<{
      isin: string;
      ticker: string;
      allocation_effect: number;
      selection_effect: number;
      interaction_effect: number;
      total_effect: number;
    }>;
  };
  factor_contribution?: {
    factors: Array<{ name: string; exposure: number; contribution: number }>;
    residual: number;
    r_squared: number;
  };
}

export interface PerformanceLedgerEntry {
  id: number;
  composite_id: string;
  as_of: string;
  twr: number;
  mwr: number;
  dispersion?: number;
  ex_post_risk: {
    volatility: number;
    sharpe: number;
    sortino: number;
    downside_volatility: number;
    max_drawdown: number;
    drawdown_duration_days: number;
    calmar: number;
    cvar_95: number;
    skewness: number;
    kurtosis: number;
  };
}

export interface DossierResponse {
  id: string;
  ts: string;
  conviction: number;
  dossier_json: string;
}

export interface StageProgress {
  stage: string;        // "retire" | "miner" | "screener" | "trader" | "dossier"
  status: string;       // "pending" | "running" | "ok" | "error"
  duration_s?: number;
  error?: string;
  count?: number | null;
  symbols?: string[] | null;
  reason?: string | null;
}

// --- AlphaCrafter diagnostics types ---

export interface Substitution {
  from_category: string;
  to_category: string;
  expected_savings?: number | null;
  reason: string;
}

export interface LLMHealthResponse {
  local_llama_up: boolean;
  anthropic_reachable: boolean;
  openai_reachable: boolean;
  last_check: string;
}

export interface Goal {
  id: string;
  user_id: string;
  title: string;
  target_amount?: number | null;
  target_date?: string | null;
  risk_tolerance?: string | null;
  asset_class_targets?: Record<string, number> | null;
  monthly_contribution?: number | null;
  progress: number;
  notes?: string | null;
  created_at: string;
}

export interface ImportValidationResponse {
  valid: boolean;
  format?: string | null;
  message: string;
  columns?: string[] | null;
  row_count?: number | null;
}

export interface ImportCommitResponse {
  created_ids: string[];
  errors: string[];
}

export interface DriftWithTargetsResponse {
  current: Record<string, number>;
  targets: TargetAllocationItem[];
  drift: Record<string, number>;
}

export interface TargetAllocationItem {
  id: string;
  asset_type: string;
  target_pct: number;
  tolerance_pct: number;
  updated_at: string | null;
}

export interface TargetAllocationResponse {
  allocations: TargetAllocationItem[];
}

export interface PaperPortfolioSummary {
  id: string;
  name: string;
  currency: string;
  initial_cash: number;
  total_value: number;
  cash_balance: number;
  securities_value: number;
  total_return_pct: number;
  // Always present as a dict key on the wire, possibly `null` when metrics
  // haven't been computed yet (services/paper_portfolio.py:get_summary) —
  // `number | null`, not optional, matches that contract exactly.
  sharpe: number | null;
  max_drawdown: number | null;
  holding_count: number;
  trade_count: number;
  /** Days of snapshot history behind sharpe/max_drawdown. */
  history_days?: number;
  seeded_from_dkb?: boolean;
  /** Start of the current run (a reset moves it). */
  inception_at?: string;
  baseline_value?: number;
  /** Gross dividends credited to cash since inception, EUR. */
  dividends_eur?: number;
  benchmark?: PaperBenchmark;
}

/** The same starting value in MSCI World EUR from the paper run's inception. */
export interface PaperBenchmark {
  symbol: string;
  start: string;
  available: boolean;
  as_of?: string;
  total_return_pct?: number;
  value?: number;
  excess_return_pct?: number;
}

/** GET /api/paper-portfolio/{id}/performance */
export interface PaperPerformance {
  summary: PaperPortfolioSummary;
  benchmark: PaperBenchmark;
  series: { date: string; value: number; benchmark: number | null }[];
  method: string;
}

/** GET /api/paper-portfolio/{id}/archives */
export interface PaperArchive {
  id: string;
  archived_at: string | null;
  inception_at: string | null;
  reason: string;
  trades: number;
  snapshots: number;
  last_total_return_pct: number | null;
}

export interface PaperHolding {
  id: string;
  ticker: string;
  name: string;
  asset_type: string;
  quantity: number;
  avg_buy_price: number;
  current_price?: number;
  market_value?: number;
  unrealized_pnl?: number;
  return_pct?: number;
  currency: string;
  weight_pct?: number;
  /** False when no EUR price was available and the line is valued at cost. */
  priced?: boolean;
  price_local?: number | null;
  quote_currency?: string | null;
}

export interface PaperTrade {
  id: string;
  ticker: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  value: number;
  fee?: number;
  date: string;
  confidence?: number;
  rationale?: string;
}

// ── LLM Portfolio (mandate A/B decision loop) — /api/llm-portfolio/* ────────
// Distinct from PaperPortfolioSummary/PaperHolding/PaperTrade above (the
// singular `/api/paper-portfolio/*` context) and from the `advisor` context
// (the daily quant-gated loop at the `/advisor` route, pages/advisor/): this
// is the mandate A/B system surfaced at `/mandates` (see pages/mandates/).
// Field shapes mirror backend/app/api/llm_portfolio.py exactly. Note the
// backend API prefix itself (`/api/llm-portfolio/*`) is unchanged — only the
// frontend route was renamed away from the misleading `/llm-portfolio`.

export interface MandateListItem {
  id: string;
  name: string;
  mandate: string;
  managed_by: string;
  // Wire values are decimal strings (Python Decimal serialised via str()).
  total_value: string;
  holdings_count: number;
  cash_balance: string;
}

export interface MandateCreateResponse {
  id: string;
  name: string;
  mandate: string;
  managed_by: string;
}

export interface MandateReviewTriggerResponse {
  job_id: string;
  status: string;
}

export interface MandateReviewJobStatus {
  job_id: string;
  /** "scheduled" while pending; "not_found_or_completed" once done or gone
   * (APScheduler drops finished jobs — this endpoint carries no result
   * payload, only a completion signal; re-fetch the journal for the outcome). */
  status: "scheduled" | "not_found_or_completed" | string;
  next_run?: string | null;
}

/** The LLM's decision JSON. Review-sourced entries use action/ticker/thesis/
 * expectation; competition-sourced entries additionally carry proposals/
 * errors/trades/score (see api/llm_portfolio.py::get_journal's merge). */
export interface MandateDecision {
  action?: string;
  ticker?: string | null;
  quantity?: number;
  thesis?: string | null;
  confidence?: number | null;
  expectation?: Record<string, unknown> | null;
  key_risks?: string[];
  alternatives_considered?: string[];
  assessment?: Record<string, unknown> | null;
  proposals?: unknown[];
  errors?: unknown[];
  trades?: unknown[];
  score?: Record<string, unknown>;
  round_number?: number;
  winner?: boolean;
}

export interface MandateJournalEntry {
  id: string;
  review_date: string | null;
  mandate: string;
  decision_json: MandateDecision;
  reflection_json: Record<string, unknown> | null;
  context_token_estimate: number | null;
  status: string;
  verdict?: string | null;
  horizon_weeks?: number | null;
  scored_at?: string | null;
  created_at: string | null;
  source: "review" | "competition";
  estimate: true;
  not_financial_advice: true;
}

export interface MandateAccuracy {
  portfolio_id: string;
  mandate: string;
  counts: { hit: number; miss: number; partial: number; unresolvable: number };
  resolved: number;
  hit_rate: number | null;
  pending_scoring: number;
  estimate: true;
  not_financial_advice: true;
}

export interface MandateMissingHolding {
  isin: string;
  ticker: string | null;
  name: string | null;
  quantity: string;
}

export interface MandateWeightDiff {
  isin: string;
  ticker: string | null;
  paper_quantity: string;
  real_quantity: string;
  quantity_diff: number;
}

export interface MandateAdviceCard {
  id: string;
  advice_text: string;
  performance_delta: number;
  status: "new" | "acknowledged" | "dismissed";
}

export interface MandateDivergence {
  paper_vs_real: {
    missing_in_real: MandateMissingHolding[];
    missing_in_paper: MandateMissingHolding[];
    weight_diffs: MandateWeightDiff[];
  };
  performance_delta: number;
  advice_cards: MandateAdviceCard[];
}

export interface MandateAdviceCardUpdateResponse {
  id: string;
  status: string;
}

export interface MandatePatchResponse {
  id: string;
  mandate: string;
  managed_by: string;
}

export interface MandateReseedResponse {
  id: string;
  holdings_count: number;
}

export type PulseStatus = "info" | "warning" | "critical";

export type AdvisorCheck = {
  type: string;
  severity: PulseStatus;
  message: string;
  detail: string;
};

export type PulseResult = {
  status: PulseStatus;
  checks: AdvisorCheck[];
  concentration: number;
  drift_count: number;
  allocation: Record<string, number>;
};

export type DeepAnalysisResult = {
  mode: "deep" | "deep_fallback";
  pulse: PulseResult;
  regime: MacroRegimeSnapshot | null;
  analysis?: {
    total_value: number;
    allocation: Record<string, number>;
    holding_count: number;
    top_holdings: { name: string; value: number; pct: number }[];
    note?: string;
  };
  llm_analysis?: string;
};

export type AdvisorResponse = {
  mode: "pulse" | "deep";
  result: PulseResult | DeepAnalysisResult;
  regime: MacroRegimeSnapshot | null;
};

export type StockContext = {
  ticker: string;
  quote?: { price?: number; change_pct?: number; name?: string; [k: string]: unknown };
  report?: { text: string; summary: string; generated_at: string; cached: boolean } | null;
  price_history?: { date: string; open: number; high: number; low: number; close: number; volume: number }[];
  news?: { title: string; source: string; published_at: string; sentiment_label?: string; sentiment_score?: number }[];
  fundamentals?: Record<string, unknown> | null;
};

export type MultiHorizonVerdict = {
  horizon: string;
  verdict: "BUY" | "HOLD" | "SELL";
  confidence: number;
  source: string;
};

export type ResearchReportResponse = {
  report: string;
  summary: string;
  context?: Record<string, unknown>;
  cached: boolean;
  generated_at: string;
};

export type VerdictsResponse = {
  ticker: string;
  verdicts: MultiHorizonVerdict[];
  generated_at: string;
};

// --- Phase 6: Piotroski F-Score types ---

export type PiotroskiSignal = {
  name: string;
  value: number | null;
  pass: boolean;
};

export type PiotroskiResponse = {
  ticker: string;
  score: number | null;
  max: number;
  signals: PiotroskiSignal[];
  source: string;
  message?: string;
};

// --- Phase 7: Sentiment types ---

export type SentimentArticle = {
  title: string;
  source: string;
  published_at: string;
  // "unscored" (a distinct literal, not "neutral") when FinBERT hasn't
  // scored this article yet; sentiment_score is null in that case too.
  sentiment_label: string;
  sentiment_score: number | null;
};

export type SentimentResponse = {
  ticker: string;
  total: number;
  positive_pct: number;
  negative_pct: number;
  neutral_pct: number;
  unscored_pct: number;
  avg_score: number;
  overall: string;
  articles: SentimentArticle[];
};

export type EtfHolding = {
  ticker: string;
  weight: number;
  name: string;
};

export type EtfProfile = {
  ticker: string;
  name: string;
  ter: number | null;
  size: number | null;
  domicile: string | null;
  replication: string | null;
  policy: string | null;
  ucits: boolean;
  top_holdings: EtfHolding[];
  sectors: Record<string, number>;
  /** Country weights of the tracked index; empty for an ETF not mapped to one. */
  regions: Record<string, number>;
  /** Where `regions` comes from, e.g. "MSCI World constituents (SPDR holdings sppw-gy as of 2026-09-29)". */
  regions_source?: string | null;
  holdings_count: number;
};

export type TrackingStats = {
  etf_ticker: string;
  index_ticker: string;
  tracking_error: number;
  beta: number;
  r_squared: number;
  annualised_return_diff: number;
  sample_days: number;
  period_years: number;
};

// --- Portfolio-Level LLM Analysis ---

export type PortfolioReportResponse = {
  report: string;
  summary: string;
  cached: boolean;
  generated_at: string;
  error?: string | null;
  context?: {
    portfolio_summary: {
      total_value: number;
      cash_value: number;
      security_value: number;
      currency: string;
      allocation_pct: Record<string, number>;
      cashflow_30d: { income: number; outflow: number; net: number };
    };
    positions: {
      name: string;
      ticker?: string;
      value: number;
      source: string;
      asset_type: string;
      pct: number;
    }[];
  } | null;
};

// --- AlphaCrafter SharedMemory types ---

export interface JobRun {
  id: string;
  job_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  error_message: string | null;
  triggered_by: string;
}

export interface JobRunsListResponse {
  runs: JobRun[];
  total: number;
}

export interface JobSummary {
  job_name: string;
  last_run: string | null;
  last_status: string | null;
  avg_duration_ms: number | null;
  total_runs: number;
  success_rate: number;
}

export async function getJobRuns(params?: {
  job_name?: string;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<JobRunsListResponse> {
  const searchParams = new URLSearchParams();
  if (params?.job_name) searchParams.set("job_name", params.job_name);
  if (params?.status) searchParams.set("status", params.status);
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.offset) searchParams.set("offset", String(params.offset));
  const query = searchParams.toString();
  return api<JobRunsListResponse>(`/api/jobs${query ? `?${query}` : ""}`);
}

export async function getJobSummary(): Promise<JobSummary[]> {
  return api<JobSummary[]>("/api/jobs/summary");
}

export interface ScheduledJob {
  id: string;
  schedule: string | null;
  next_run: string | null;
  last_run: string | null;
  last_status: string | null;
  last_error: string | null;
  avg_duration_ms: number | null;
  total_runs: number;
  success_rate: number | null;
}

export interface JobScheduleResponse {
  worker: { started_at: string; beat_at: string; alive: boolean } | null;
  jobs: ScheduledJob[];
  retired: ScheduledJob[];
}

export const getJobSchedule = () => api<JobScheduleResponse>("/api/jobs/schedule");

// --- Phase 3: Discover types ------------------------------------------------

export interface DiscoverRunSummary {
  id: string;
  status: string;
  created_at: string;
  completed_at: string | null;
  stage_summary: string;
}

export interface DiscoverCandidate {
  id: string;
  symbol: string;
  isin: string | null;
  name: string | null;
  source: string;
  status: string;
  reject_stage: string | null;
  reject_reason: string | null;
  scores: Record<string, any>;
  tradeable: Record<string, any>;
  dossier_id: string | null;
  recommendation_id: string | null;
  // Dossier-derived fields (available after Task 4+5)
  conviction?: number | null;
  horizon_months?: number | null;
  expected_return?: number | null;
  // Cohort track-record gate status ("insufficient_data" | "proven" |
  // "unproven") — null when the candidate has no recommendation yet, or
  // predates this gate.
  track_record_status?: string | null;
  // True only when track_record_status === "unproven" — auto-withheld
  // rather than auto-approved. Withheld candidates stay visible with this
  // badge; never silently hidden.
  withheld?: boolean;
}

/**
 * Defensive accessor for `candidate.scores.concerns`. Total function: missing
 * or malformed `scores` (older runs) yields `[]`, never throws. Capped at 12
 * entries so a pathological payload cannot flood the UI.
 */
export function parseConcerns(scores?: Record<string, unknown> | null): string[] {
  const raw = scores?.concerns;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((c): c is string => typeof c === "string" && c.trim().length > 0)
    .slice(0, 12);
}

export interface DiscoverRunDetail {
  id: string;
  status: string;
  stage_json: Record<string, any>;
  params_json: Record<string, any>;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
  shortlisted: DiscoverCandidate[];
  rejected: DiscoverCandidate[];
  candidates: DiscoverCandidate[];
}

export interface DiscoverRunDebug {
  run_id: string;
  status: string;
  error_message: string | null;
  created_at: string | null;
  completed_at: string | null;
  age_seconds: number | null;
  last_progress_elapsed_seconds: number | null;
  seconds_since_last_progress: number | null;
  stage: Record<string, any>;
  candidates: {
    total: number;
    by_status: Record<string, number>;
    by_reject_stage: Record<string, number>;
  };
  scheduler_job: Record<string, any>;
  hint: string;
}

export interface DiscoverReturnBand {
  p10: number;
  p50: number;
  p90: number;
  method: string;
  n_paths?: number;
  estimate: boolean;
  not_tax_advice: boolean;
  concerns?: string[];
}

/** One composite input as applied (ADR 0017): `weight` is the renormalised
 *  weight actually used, and contributions sum to the composite score. */
export interface DiscoverCompositeInput {
  signal: string;
  score: number;
  weight: number;
  contribution: number;
}

/** How the expected-return point estimate was built (ADR 0017). Percent
 *  numbers (2.44 = 2.44%) except `beta`/`beta_raw` and `credibility_weight`
 *  (a fraction). Only `er_annual_pct` is present for blocks/BL estimates. */
export interface DiscoverReturnComponents {
  anchor_pct?: number;
  risk_free_pct?: number;
  beta?: number | null;
  beta_raw?: number | null;
  beta_source?: "weekly" | "daily" | null;
  equity_premium_pct?: number;
  prior_pct?: number;
  credibility_weight?: number;
  tilt_pct?: number;
  er_annual_pct: number;
}

export interface DiscoverDossier {
  /**
   * Wire unit conventions for numeric members of this payload:
   * - `expected_return` and any `signal_breakdown` key suffixed `_pct`
   *   (e.g. `expected_return_anchor_pct`) are percent numbers (44.5 = 44.5%).
   * - `expected_return_band` members (p10/p50/p90) are fractions (0.445 =
   *   44.5%) — multiply by 100 before comparing against percent numbers.
   */
  id: string;
  conviction: number;
  direction: "long" | "short" | "neutral";
  horizon_months: number;
  expected_return: number | null;
  /**
   * Which backward-looking signal `expected_return` was anchored on. The union
   * must match the kinds `_resolve_expected_return` emits — it previously
   * named `trailing_12_1m_momentum`, a string the backend has never emitted,
   * and omitted both kinds it does emit, so the drawer's disclaimer never
   * rendered for them. M5 adds `building_blocks_credibility` and
   * `bl_posterior` (Options 1/2).
   */
  expected_return_anchor_kind?:
    | "trailing_3y_annualized_return"
    | "momentum_12_1m"
    | "trailing_1m_annualized_return"
    | "building_blocks_credibility"
    | "bl_posterior"
    | null;
  /** PRIIPs-style P10/P50/P90 band around the anchor (M5 Option 0); null when
   *  no anchor/db history supports it. Mirrors DossierResponse on the backend. */
  expected_return_band?: DiscoverReturnBand | null;
  expected_return_components?: DiscoverReturnComponents | null;
  /** Every composite input, largest contribution first; null for dossiers
   *  written before ADR 0017. */
  composite_breakdown?: DiscoverCompositeInput[] | null;
  thesis: string;
  key_risks: string[];
  /** Mostly numbers; `regime_state` and `expected_return_anchor_kind` are strings. */
  signal_breakdown: Record<string, number | string | null>;
  /** Non-directional macro/volatility regime context (substitute signal for
   *  passive ETFs whose directional ML signal is unavailable by design). */
  regime_context?: Record<string, any> | null;
  estimate: boolean;
  not_financial_advice: boolean;
  generated_by?: "llm" | "fallback";
  fallback_reason?: string | null;
  created_at: string | null;
}

export const getRecentRuns = (limit = 50) =>
  api<{ runs: RecentRun[] }>(`/api/quant/runs/recent?limit=${limit}`).then((r) => r.runs);
export interface DiscoverySkillSnapshot {
  id: string;
  snapshot_at: string | null;
  metric_type: string;
  total_predictions: number;
  total_resolved: number;
  hit_rate: number | null;
  brier_score_avg: number | null;
  rank_ic: number | null;
  icir: number | null;
  ece: number | null;
  mincer_a0: number | null;
  mincer_a1: number | null;
}

export const getSkillTrend = () => api<DiscoverySkillSnapshot[]>("/api/discover/skill-trend");

/** Skill over all resolved predictions, judged against the passive core. */
export interface DiscoverySkillSummary {
  benchmark: string;
  resolved: number;
  resolved_vs_benchmark: number;
  pending: number;
  next_resolve_at: string | null;
  hit_rate: number | null;
  mean_excess_return: number | null;
  mean_rank_ic: number | null;
  icir: number | null;
  ic_t_stat: number | null;
  ic_dates: number;
  min_ic_names: number;
  /** Newey-West lags behind ic_t_stat (dates inside one horizon). */
  ic_nw_lags?: number;
  /** 95 % half-width of hit_rate, clustered by date; null before min_independent_windows. */
  hit_rate_ci_half_width?: number | null;
  /** Non-overlapping horizons the resolved record spans. */
  independent_windows?: number;
  min_independent_windows?: number;
  horizon_calendar_days?: number;
}

export const getSkillSummary = () => api<DiscoverySkillSummary>("/api/discover/skill-summary");

// --- Config Registry (Discover) -------------------------------------------

export interface ConfigResponse {
  id: string;
  config_type: "signal_weights" | "prompt_template" | "universe";
  version_label: string;
  description: string | null;
  config_json: Record<string, unknown>;
  status: "active" | "challenger" | "champion" | "retired" | "failed";
  source: "manual" | "perturbation" | "system";
  parent_config_id: string | null;
  created_at: string | null;
  champion_at: string | null;
  metrics_json: Record<string, unknown> | null;
  is_stale: boolean;
}

export interface ConfigReviewResponse {
  id: string;
  reviewed_at: string | null;
  config_type: string;
  champion_id: string;
  promoted_id: string | null;
  challenger_results: Record<string, unknown>[];
  rejected_ids: string[];
}

export interface PerturbResponse {
  created: ConfigResponse[];
}

export const getConfigs = (configType?: string, status?: string) => {
  const params = new URLSearchParams();
  if (configType) params.set("config_type", configType);
  if (status) params.set("status", status);
  const qs = params.toString();
  return api<ConfigResponse[]>(`/api/discover/configs${qs ? `?${qs}` : ""}`);
};

export const createConfig = (cfg: {
  config_type: string;
  config_json: Record<string, unknown>;
  version_label?: string;
  description?: string;
}) =>
  api<ConfigResponse>("/api/discover/configs", {
    method: "POST",
    body: JSON.stringify(cfg),
  });

export const activateConfig = (id: string) =>
  api<ConfigResponse>(`/api/discover/configs/${id}/activate`, { method: "POST" });

export const syncConfigToDefault = (id: string) =>
  api<ConfigResponse>(`/api/discover/configs/${id}/sync-to-default`, { method: "POST" });

export const perturbConfigs = (configType = "signal_weights", nVariants = 5) =>
  api<PerturbResponse>(
    `/api/discover/configs/perturb?config_type=${configType}&n_variants=${nVariants}`,
    { method: "POST" },
  );

export const getConfigReviews = () =>
  api<ConfigReviewResponse[]>("/api/discover/configs/reviews");

export const triggerReview = () =>
  api<{ status: string }>("/api/discover/configs/review", { method: "POST" });

// --- LLM Portfolio types --------------------------------------------------

export interface GraduationCriterion {
  key: string;
  label: string;
  passed: boolean;
  progress: number; // 0..1
  value: number | null;
  target: number | null;
  detail: string;
  blocking: boolean;
}

export interface PassiveCoreInfo {
  mode: string;
  ticker: string;
  isin: string;
  message: string;
}

// --- "This month" plan (GET /api/plan/month) --------------------------------

export type PlanSleeveKey = "core" | "tilt" | "satellite";

export interface PlanPosition {
  isin: string;
  ticker: string | null;
  name: string;
  value_eur: number;
}

export interface PlanSleeve {
  key: PlanSleeveKey;
  label: string;
  current_eur: number;
  current_pct: number;
  target_pct: number;
  max_pct: number;
  unlocked: boolean;
  status: string;
  contribution_eur: number;
  after_eur: number;
  after_pct: number;
  positions: PlanPosition[];
  /** Sold past the drift band this month, and reinvested from other sleeves' sales. */
  sale_eur?: number;
  reinvest_eur?: number;
}

export interface PlanAction {
  sleeve: PlanSleeveKey;
  /** "one_off" is optional: cash above the emergency reserve. */
  kind: "savings_plan" | "order" | "sale" | "one_off";
  amount_eur: number;
  instrument: string;
  ticker: string | null;
  isin: string | null;
  note: string;
  /** Where to place it: a buy's broker, or the depot a sale comes from. */
  broker?: string | null;
  broker_label?: string | null;
  /** The depot a sale comes from, when one broker holds the position in several. */
  account_id?: string | null;
}

export interface RunningSavingsPlan {
  broker: string;
  broker_label: string;
  name: string;
  isin: string | null;
  sleeve: PlanSleeveKey;
  amount_eur: number;
  frequency: string;
  /** Null for a schedule the plan does not know (left out of the totals). */
  monthly_eur: number | null;
  next_execution_date: string | null;
}

export interface PlanCash {
  savings_eur: number;
  broker_cash_eur: number;
  broker_cash_by_broker?: Record<string, number>;
  emergency_reserve_eur: number;
  reserve_set: boolean;
  investable_eur: number;
}

export interface MonthlyPlan {
  month: string;
  generated_at: string;
  headline: string;
  no_change: boolean;
  contribution_eur: number;
  book_eur: number;
  holdings_synced_at: string | null;
  has_holdings: boolean;
  tracking_error_budget: { label: string; tilt_max_pct: number; satellite_max_pct: number };
  min_order_eur: number;
  drift_band_pp?: number | null;
  contribution_mode?: "savings_plan" | "manual_orders";
  broker?: "dkb" | "scalable";
  broker_label?: string;
  broker_choice?: "auto" | "dkb" | "scalable";
  brokers_connected?: string[];
  sleeves: PlanSleeve[];
  actions: PlanAction[];
  savings_plans?: { items: RunningSavingsPlan[]; monthly_eur: number; by_sleeve: Record<string, number> } | null;
  cash?: PlanCash | null;
  /** Emerging-markets share of the core, looking through the funds. */
  /** em_pct is null when no core fund has a known emerging-markets split. */
  core_look_through?: { em_eur: number; em_pct: number | null; world_em_pct: number; unknown_eur: number } | null;
  notes?: string[];
  never_sells: boolean;
  not_investment_advice: boolean;
}

export async function getMonthlyPlan(): Promise<MonthlyPlan> {
  return api<MonthlyPlan>("/api/plan/month");
}

export interface GraduationStatus {
  graduated: boolean;
  overall_progress: number;
  criteria: GraduationCriterion[];
  metrics: Record<string, number | string | null>;
  generated_at: string;
  estimate: boolean;
  not_tax_advice: boolean;
  not_financial_advice: boolean;
  recommendations_unlocked: boolean;
  passive_core: PassiveCoreInfo;
  state?: {
    graduated: boolean;
    since: string | null;
    last_transition_at: string | null;
    last_reason: string | null;
    strategy_id: string | null;
  };
}

export interface GraduationTransition {
  id: string;
  strategy_id: string | null;
  from_graduated: boolean;
  to_graduated: boolean;
  reason: string;
  criteria: Record<string, { passed: boolean; value: number | null; target: number | null }>;
  created_at: string | null;
}

// --- Advisor loop diagnostics ---

export type AdvisorDiagnosticStatus = "ok" | "warn" | "fail" | "skipped";

export interface AdvisorDiagnosticCheck {
  key: string;
  label: string;
  status: AdvisorDiagnosticStatus;
  detail: string;
  data: Record<string, any>;
  remedy: string | null;
}

export interface AdvisorDiagnosticStage {
  key: string;
  label: string;
  detail: string;
  remedy: string | null;
}

export interface AdvisorDiagnosticsReport {
  status: AdvisorDiagnosticStatus;
  generated_at: string;
  user_id: string;
  summary: string;
  counts: Record<AdvisorDiagnosticStatus, number>;
  blocking: AdvisorDiagnosticStage[];
  /** Stages held up only by an unelapsed prediction horizon — waiting, not broken. */
  pending: AdvisorDiagnosticStage[];
  checks: AdvisorDiagnosticCheck[];
}

// --- Advisor evolution (PR2) ---

export interface AdvisorScorecardPayload {
  window_start: string;
  window_end: string;
  n_predictions: number;
  n_resolved: number;
  risk_adjusted_return: {
    sharpe: number | null;
    sortino: number | null;
    calmar: number | null;
    /** Lo (2002) standard error of the annualised Sharpe. A bare Sharpe point
     * estimate is misleading at the sample sizes this app has — after a full
     * year of daily NAV the 95% interval on a Sharpe of 1.0 is still ~±2.0 —
     * so render the band, not the point. */
    sharpe_se: number | null;
    /** True when |sharpe| < 1.96 * sharpe_se, i.e. the figure is statistically
     * indistinguishable from zero and must not be used to rank anything. */
    sharpe_ci_spans_zero: boolean | null;
    n_nav_returns: number | null;
  };
  calibration: { brier_avg: number | null; log_loss_avg: number | null };
  magnitude_accuracy: { mz_slope: number | null; mz_r2: number | null };
  downside_discipline: { max_drawdown: number | null; cvar_95: number | null };
  computed_at: string | null;
}

export interface EvolutionStrategy {
  id: string;
  role: string;
  portfolio_id: string | null;
  config: Record<string, unknown>;
  parent_strategy_id: string | null;
  promoted_at: string | null;
  created_at: string | null;
  scorecard: AdvisorScorecardPayload | null;
  composite: number | null;
  composite_detail: { components?: Record<string, number>; note?: string };
  /** ISO datetime of the earliest still-pending prediction's resolve date for
   * this sleeve, or null if there's nothing left to resolve. */
  next_resolution_at: string | null;
}

export interface EvolutionLesson {
  id: string;
  strategy_id: string;
  text: string;
  tags: { signals?: string[]; regime?: string; sectors?: string[] };
  rank: number;
  active: boolean;
  created_at: string | null;
}

export interface EvolutionStatus {
  champion: EvolutionStrategy | null;
  challenger: EvolutionStrategy | null;
  rounds: {
    run_id: string;
    status: string;
    started_at: string | null;
    ended_at: string | null;
    decisions: {
      round: number;
      portfolio_id: string;
      winner: boolean;
      score: { composite?: number | null; components?: Record<string, number>; n_resolved?: number };
      created_at: string | null;
    }[];
  }[];
  promotion_history: {
    strategy_id: string;
    role: string;
    promoted_at: string | null;
    retired_at: string | null;
    parent_strategy_id: string | null;
  }[];
  lessons: EvolutionLesson[];
}

// --- Advisor loop (autonomous paper-trade cycle) --------------------------

export interface AdvisorTrade {
  id: string;
  ticker: string;
  side: string;
  quantity: number;
  price: number;
  value: number;
  fee: number;
  confidence: number | null;
  rationale: string | null;
  executed_at: string | null;
}

export interface AdvisorTradeFlow {
  cash_before: number;
  turnover_budget: number;
  turnover_used: number;
  max_turnover_pct: number;
  min_ticket_eur: number;
  fees_paid: number;
}

export interface AdvisorDecisionEntry {
  ticker: string;
  action: string;
  target_weight: number;
  thesis: string;
  confidence: number;
  confidence_raw?: number | null;
  confidence_calibrated?: number | null;
}

export interface AdvisorCycleResponse {
  portfolio_id: string | null;
  decision: {
    review_date: string | null;
    status: string;
    error: string | null;
    decisions?: AdvisorDecisionEntry[];
    blocked?: { symbol: string; reason: string }[];
    risk_envelope?: Record<string, number | null>;
    optimizer_status?: string;
    notes?: string[];
    trade_flow?: AdvisorTradeFlow;
    skipped?: { ticker: string; action: string; reason: string }[];
    funding_sells?: { ticker: string; value: number; fee: number; reason: string }[];
  } | null;
  trades: AdvisorTrade[];
  /** The 4-axis scorecard, reused verbatim from `AdvisorScorecardPayload`
   * above — these are the same portfolio-level Sharpe/Sortino/Calmar as
   * `AdvisorScorecardPayload.risk_adjusted_return`, computed over the
   * paper-portfolio's own NAV returns (unlike a prediction-accuracy metric,
   * which would be computed over forecast-vs-outcome deltas instead). */
  scorecard: AdvisorScorecardPayload | null;
}

export interface WhatWouldChangeRow {
  ticker: string;
  name: string | null;
  isin: string | null;
  real_weight: number;
  champion_weight: number;
  delta: number;
  action: "new" | "add" | "trim" | "exit";
  confidence_raw: number | null;
  confidence_calibrated: number | null;
  thesis: string | null;
  mc: { p5: number | null; p50: number | null; p95: number | null } | null;
  rank_score: number;
}

export interface WhatWouldChangeResponse {
  graduated: boolean;
  champion_strategy_id: string | null;
  champion_portfolio_id?: string;
  rows: WhatWouldChangeRow[];
  real_total_eur?: number;
  champion_total_eur?: number;
  note?: string;
  /** Always false since Phase 4: the LLM loop is paper-only. */
  actionable?: boolean;
  research_only?: string;
  estimate: boolean;
  not_financial_advice: boolean;
}

export interface GraduatedRecommendation {
  ticker: string;
  action: string;
  confidence: string;
  thesis: string;
  risks: string[];
}

export interface GraduatedRecommendationsResponse {
  report_id: string | null;
  recommendations: GraduatedRecommendation[];
  not_financial_advice: boolean;
}

/**
 * Fetches the current graduation status.
 *
 * @returns The current graduation status.
 */
export async function getGraduationStatus(): Promise<GraduationStatus> {
  return api<GraduationStatus>("/api/graduation/status");
}

/**
 * Generates graduated portfolio recommendations.
 *
 * @returns A `GraduatedRecommendationsResponse` containing the generated recommendations.
 */
export async function generateGraduatedRecommendations(): Promise<GraduatedRecommendationsResponse> {
  return api<GraduatedRecommendationsResponse>("/api/graduation/recommendations", { method: "POST" });
}

// --- ADR 0015 Phase 1/2: trial ledger evidence -----------------------------

export interface NTrialsSummaryResponse {
  /** What every Deflated Sharpe gate uses: the real ledger count. */
  resolved: number;
  floor: number;
  raw_count: number;
  /** Independent search families: a lower bound on independent tests. */
  effective?: number;
  by_context: Record<string, number>;
  hurdle_observations?: number;
  /** Annualised Sharpe the best of N skill-less trials reaches by luck. */
  hurdle_curve?: Array<{ n_trials: number; sharpe_hurdle: number }>;
}

export interface TrialLedgerEntryDto {
  id: string;
  context: string;
  trial_key: string;
  metadata: Record<string, unknown>;
  registered_at: string;
  created_at: string;
}

/** One pre-registered factor strategy's prior-informed test (report Phase 3). */
export interface FactorEvidenceCard {
  strategy: string;
  label: string;
  region: string;
  citation: string;
  etf_hint: string;
  months: number;
  start: string | null;
  end: string | null;
  long_short_annual: number | null;
  long_short_t: number | null;
  post_publication_months: number;
  post_publication_annual: number | null;
  long_only_annual: number | null;
  expected_annual: number | null;
  net_expected_annual: number | null;
  tracking_error_annual: number | null;
  worst_5y_excess: number | null;
  book_worst_5y_lag: number | null;
  tilt_cap_pct: number;
  passed: boolean;
  checks: { name: string; label: string; value: number | null; passed: boolean }[];
  yearly_long_only: Record<string, number>;
  computed_at: string | null;
}

export const getFactorPremia = () => api<FactorEvidenceCard[]>("/api/evidence/factor-premia");

export async function getNTrialsSummary(): Promise<NTrialsSummaryResponse> {
  return api<NTrialsSummaryResponse>("/api/evidence/n-trials");
}

export async function getTrialLedger(params?: {
  context?: string;
  limit?: number;
}): Promise<TrialLedgerEntryDto[]> {
  const searchParams = new URLSearchParams();
  if (params?.context) searchParams.set("context", params.context);
  if (params?.limit) searchParams.set("limit", String(params.limit));
  const query = searchParams.toString();
  return api<TrialLedgerEntryDto[]>(`/api/evidence/trial-ledger${query ? `?${query}` : ""}`);
}

export type NotificationSeverity = "info" | "warning" | "critical";

export interface NotificationItem {
  id: string;
  source: string;
  title: string;
  body?: string | null;
  severity: NotificationSeverity;
  href?: string | null;
  read_at?: string | null;
  created_at?: string | null;
}

export interface NotificationsResponse {
  items: NotificationItem[];
  unread_count: number;
}

// Trailing slash required: the backend route is defined at the router root
// ("/"), and FastAPI's redirect-slash response echoes its own bind address
// in the Location header -- fine for a same-origin client, but a dev-server
// proxy (and any reverse proxy that doesn't rewrite Location) sends the
// browser to that literal host, outside the cookie's origin, turning every
// call into a silent 401.
export const listNotifications = () => api<NotificationsResponse>("/api/notifications/");

export const markNotificationRead = (id: string) =>
  api<NotificationItem>(`/api/notifications/${id}/read`, { method: "POST" });

export const markAllNotificationsRead = () =>
  api<{ updated: number }>("/api/notifications/read-all", { method: "POST" });

// --- Decide: recommendations waiting for Accept / Reject / Snooze ------------

export type AdvisorFeedbackAction = "accepted" | "rejected" | "snoozed";

export interface PendingRecommendation {
  id: string;
  ticker: string | null;
  name: string | null;
  verdict: string;
  /** 0-1. */
  confidence: number;
  horizon: string | null;
  mode: string | null;
  approval_state: string;
  created_at: string | null;
  expires_at: string | null;
  summary: string;
  /** Back in the list because its snooze has run out. */
  resurfaced: boolean;
  isin?: string | null;
  side?: "buy" | "sell";
  /** Where to place the order yourself; nothing is ordered for you. */
  links?: BrokerLink[];
}

export interface BrokerLink {
  broker: string;
  label: string;
  url: string;
}

export interface AcceptedRecommendation {
  id: string;
  ticker: string | null;
  name: string | null;
  isin: string | null;
  side: "buy" | "sell";
  accepted_at: string | null;
  links: BrokerLink[];
  executed_at?: string | null;
  broker?: string | null;
  broker_label?: string | null;
  units?: number | null;
}

/** GET /api/portfolio/advisor/accepted */
export interface AcceptedRecommendations {
  waiting: AcceptedRecommendation[];
  executed: AcceptedRecommendation[];
}

export interface PendingRecommendationsResponse {
  items: PendingRecommendation[];
  /** Every pending item, not just the ones in `items`. */
  total: number;
}

// --- Admin: DKB data repair (T1.5) ----------------------------------------

export interface RepairPositionIdentity {
  name: string | null;
  ticker: string | null;
}

export interface RepairedPosition {
  isin: string | null;
  account_id: string;
  before: RepairPositionIdentity;
  after: RepairPositionIdentity;
  repaired_via: "asset" | "security_master" | "skipped";
}

export interface DkbDataRepairReport {
  dry_run: boolean;
  use_external_lookup: boolean;
  positions: RepairedPosition[];
  positions_repaired: number;
  asset_type_changes: Record<string, number>;
}

export const runDkbDataRepair = (payload: { dry_run?: boolean; use_external_lookup?: boolean }) =>
  api<DkbDataRepairReport>("/api/admin/repair/dkb-data", {
    method: "POST",
    body: JSON.stringify({ dry_run: true, use_external_lookup: false, ...payload }),
  });

export const previewDkbDataRepair = () =>
  api<DkbDataRepairReport>("/api/admin/repair/dkb-data?dry_run=true");

// --- Scalable Capital (read-only sync through the sc CLI) -------------------

export type ScalableState =
  | "disabled"
  | "running"
  | "never_synced"
  | "not_installed"
  | "login_required"
  | "guard_unattested"
  | "error"
  | "ready";

export interface ScalableStatus {
  source: string;
  enabled: boolean;
  state: ScalableState;
  message: string;
  error_code: string | null;
  last_sync_at: string | null;
  last_success_at: string | null;
  stale: boolean;
  portfolio_id: string | null;
  tracking_since: string | null;
  positions: number;
  pending_reconciliation: number;
  read_only: boolean;
}

export interface ScalableSyncLog {
  id: string;
  source: string;
  trigger: string;
  state: "running" | "success" | "warning" | "error";
  error_code: string | null;
  message: string;
  counts: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
}

export interface ScalableSavingsPlan {
  isin: string | null;
  name: string;
  amount: string | null;
  frequency: string | null;
  day_of_month: number | null;
  next_execution_date: string | null;
  kind: string;
}

export interface ScalableReconcileRow {
  holding_id: string;
  isin: string;
  name: string;
  manual_quantity: string;
  manual_value: string;
  scalable_quantity: string;
  scalable_value: string | null;
  decision: "replace" | "keep_both" | null;
  quantity_matches: boolean;
}

export type ScalableDecision = "replace" | "keep_both";

export interface ScalableLogin {
  state: "idle" | "waiting" | "succeeded" | "failed" | "expired" | "cancelled";
  /** Only ever an https *.scalable.capital link; otherwise null. */
  verification_url: string | null;
  /** The host sc printed; show it as plain text when `verification_url` is null. */
  verification_host: string | null;
  user_code: string | null;
  message: string | null;
  /** E.g. sudo_not_configured, sandbox_blocks_sudo, not_installed, wrapper_outdated, expired, busy, login_failed. */
  error_code: string | null;
  read_only_confirmed: boolean;
  started_at: string | null;
  finished_at: string | null;
}

export const getScalableStatus = () => api<ScalableStatus>("/api/scalable/status");
/** Starts `sc login --local-read-only` on the server; waits up to ~20 s for the code. */
export const startScalableLogin = () => api<ScalableLogin>("/api/scalable/login", { method: "POST" });
export const getScalableLogin = () => api<ScalableLogin>("/api/scalable/login");
export const cancelScalableLogin = () => api<ScalableLogin>("/api/scalable/login", { method: "DELETE" });
/** Pins the SHA-256 of the installed sc so a swapped binary is refused. */
export const pinScalableBinary = () =>
  api<{ sha256: string; pinned: boolean }>("/api/scalable/pin-binary", { method: "POST" });
export const syncScalable = () => api<ScalableSyncLog>("/api/scalable/sync", { method: "POST" });
export const getScalableSavingsPlans = () => api<ScalableSavingsPlan[]>("/api/scalable/savings-plans");
export const previewScalableReconcile = () => api<ScalableReconcileRow[]>("/api/scalable/reconcile");
export const applyScalableReconcile = (decisions: Record<string, ScalableDecision>) =>
  api<{ replaced: number; kept: number }>("/api/scalable/reconcile", {
    method: "POST",
    body: JSON.stringify({ decisions }),
  });

/** GET /api/quant/backtest/strategies */
export interface BacktestStrategy {
  key: string;
  label: string;
  about: string;
  defaults: Record<string, number>;
  grid_size: number;
}

export interface BacktestRunRequest {
  ticker: string;
  strategy: string;
  params?: Record<string, number>;
  years?: number;
  commission_bps?: number;
  spread_bps?: number;
  fund_class?: "aktien" | "misch" | "immobilien" | "other";
  apply_tax?: boolean;
}

export interface BacktestLineStats {
  total_return: number;
  cagr: number | null;
  volatility: number | null;
  sharpe: number | null;
  max_drawdown: number;
  exposure?: number;
  trades?: number;
  costs_eur?: number;
  taxes_eur?: number;
}

export interface BenchmarkRegression {
  n: number;
  alpha: number | null;
  alpha_t_stat: number | null;
  beta: number | null;
  beta_t_stat: number | null;
  r_squared: number | null;
  tracking_error: number | null;
  information_ratio: number | null;
}

/** POST /api/quant/backtest/run */
export interface BacktestRun {
  ticker: string;
  strategy: string;
  label: string;
  params: Record<string, number>;
  available: boolean;
  reason?: string;
  currency: "EUR";
  start?: string;
  end?: string;
  years?: number;
  warmup_days?: number;
  benchmark?: string | null;
  risk_free?: number;
  series?: { date: string; strategy: number; buy_hold: number; benchmark: number | null; drawdown: number }[];
  trades?: { date: string; side: "buy" | "sell"; price_eur: number }[];
  stats?: { strategy: BacktestLineStats; buy_hold: BacktestLineStats; benchmark: BacktestLineStats | null };
  vs_buy_hold?: BenchmarkRegression;
  vs_benchmark?: BenchmarkRegression | null;
  evidence?: {
    verdict: "evidence" | "insufficient_evidence" | "too_short" | "reference";
    why: string;
    n_trials: number;
    n_trials_effective: number;
    dsr: number | null;
    dsr_effective: number | null;
    dsr_threshold: number;
    pbo: number | null;
    pbo_threshold: number;
    pbo_grid_size: number;
    min_track_record_years: number | null;
  };
  assumptions?: Record<string, string>;
  estimate?: true;
}
