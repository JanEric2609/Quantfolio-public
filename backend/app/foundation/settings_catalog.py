"""Declarative settings catalog -- single source of truth for Control Center UI.

Every public setting key and every sensitive integration is documented here
with the Control Center page (``group``) it lives on, the section heading it
sits under, its label, help text, input type, unit and status. Connections
(``CONNECTIONS``) bundle a credential with the public settings that configure
the same service, so the UI can render them in one drawer.

The catalog is served by ``GET /api/settings/schema``.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Status = Literal["active", "needs_worker_restart", "experimental"]
InputType = Literal["text", "number", "boolean", "password", "select", "json", "date", "provider_chain"]
# How a value is displayed. ``fraction_pct`` is stored as a fraction (-0.05)
# and shown and entered as a percentage (-5 %); every other unit is a suffix
# only and the stored value is what the user types.
Unit = Literal["fraction_pct", "pct", "pp", "eur", "days", "hours", "seconds", "points"]

# Control Center pages. ``area`` is the sidebar group the page sits under.
PAGES: list[dict[str, str]] = [
    {"group": "profile", "path": "profile", "area": "you", "label": "Profile & monthly plan",
     "description": "Who you are as an investor and how much you invest each month."},
    {"group": "tax", "path": "tax", "area": "you", "label": "Tax",
     "description": "Inputs for the German tax estimates in the Tax Cockpit. Estimates only, not tax advice."},
    {"group": "banking", "path": "bank", "area": "connections", "label": "Banks & brokers",
     "description": "Read-only sync with DKB over FinTS and with Scalable Capital through its official CLI."},
    {"group": "market_data", "path": "market-data", "area": "connections", "label": "Market data",
     "description": "Price, fundamentals and macro data providers, and the order they are asked in."},
    {"group": "ai", "path": "ai", "area": "connections", "label": "AI models",
     "description": "The local LLM that runs research and chat, and optional cloud models."},
    {"group": "notes_alerts", "path": "notes-alerts", "area": "connections", "label": "Notes & alerts",
     "description": "Obsidian vault, Telegram bot and e-mail delivery."},
    {"group": "discover", "path": "discover", "area": "engine", "label": "Discover & decision loop",
     "description": "How candidates are scored and whether real-money guidance may leave the passive core."},
    {"group": "regime", "path": "regime", "area": "engine", "label": "Market regime",
     "description": "The model that labels the market bull, neutral or bear, and the crisis override."},
    {"group": "research", "path": "research", "area": "engine", "label": "AlphaCrafter & Quant Lab",
     "description": "Factor mining, the trader sweep and the lab's signal gate."},
    {"group": "verification", "path": "verification", "area": "engine", "label": "Risk alerts",
     "description": "The thresholds at which the Portfolio → Risk page raises drawdown and concentration alerts."},
    {"group": "security", "path": "security", "area": "system", "label": "Security & access",
     "description": "Passkeys, secret encryption and which web origins may call the API."},
    {"group": "system", "path": "storage", "area": "system", "label": "Storage & performance",
     "description": "Where the worker keeps its caches and how much memory DuckDB may use."},
]
PAGE_GROUPS = {page["group"] for page in PAGES}
PAGE_PATHS = {page["group"]: page["path"] for page in PAGES}


class CatalogEntry:
    """A single setting or integration documented in the catalog."""

    __slots__ = (
        "key", "group", "label", "help", "doc_url",
        "input_type", "sensitive", "status", "options",
        "extended_help", "setup_steps",
        "section", "advanced", "unit", "option_labels", "connection",
        "placeholder", "min", "max",
    )

    def __init__(
        self,
        key: str,
        group: str,
        label: str,
        help: str,
        doc_url: str | None = None,
        input_type: InputType = "text",
        sensitive: bool = False,
        status: Status = "active",
        options: list[str] | None = None,
        extended_help: str | None = None,
        setup_steps: list[str] | None = None,
        *,
        section: str | None = None,
        advanced: bool = False,
        unit: Unit | None = None,
        option_labels: dict[str, str] | None = None,
        connection: str | None = None,
        placeholder: str | None = None,
        min: float | None = None,
        max: float | None = None,
    ) -> None:
        self.key = key
        self.group = group
        self.label = label
        self.help = help
        self.doc_url = doc_url
        self.input_type = input_type
        self.sensitive = sensitive
        self.status = status
        self.options = options
        self.extended_help = extended_help
        self.setup_steps = setup_steps
        self.section = section
        self.advanced = advanced
        self.unit = unit
        self.option_labels = option_labels
        self.connection = connection
        self.placeholder = placeholder
        self.min = min
        self.max = max

    def to_dict(self) -> dict[str, Any]:
        # ``default`` is added by the schema endpoint: the catalog must not
        # import foundation.settings, which imports it (cycle ledger).
        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "help": self.help,
            "doc_url": self.doc_url,
            "input_type": self.input_type,
            "sensitive": self.sensitive,
            "status": self.status,
            "options": self.options,
            "extended_help": self.extended_help,
            "setup_steps": self.setup_steps,
            "section": self.section,
            "advanced": self.advanced,
            "unit": self.unit,
            "option_labels": self.option_labels,
            "connection": self.connection,
            "placeholder": self.placeholder,
            "min": self.min,
            "max": self.max,
        }


class MetaField:
    """An extra field stored next to a connection's credential.

    ``sensitive`` meta is never written to plaintext ``meta_json``: the
    settings API packs it into the encrypted credential value instead.
    """

    __slots__ = ("key", "label", "sensitive", "placeholder")

    def __init__(self, key: str, label: str, *, sensitive: bool = False, placeholder: str | None = None) -> None:
        self.key = key
        self.label = label
        self.sensitive = sensitive
        self.placeholder = placeholder

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "sensitive": self.sensitive, "placeholder": self.placeholder}


class Connection:
    """An external service: its credential (if any) plus the settings that configure it."""

    __slots__ = (
        "id", "group", "label", "description", "secret", "secret_label", "secret_optional",
        "meta_fields", "enabled_key", "testable", "panel", "optional_settings",
    )

    def __init__(
        self,
        id: str,
        group: str,
        label: str,
        description: str,
        *,
        secret: str | None = None,
        secret_label: str = "API key",
        secret_optional: bool = False,
        meta_fields: list[MetaField] | None = None,
        enabled_key: str | None = None,
        testable: bool = True,
        panel: str | None = None,
        optional_settings: list[str] | None = None,
    ) -> None:
        self.id = id
        self.group = group
        self.label = label
        self.description = description
        self.secret = secret
        self.secret_label = secret_label
        self.secret_optional = secret_optional
        self.meta_fields = meta_fields or []
        self.enabled_key = enabled_key
        self.testable = testable
        self.panel = panel
        # Text settings that may stay empty. Every other text or number setting
        # must be filled before the connection counts as set up.
        self.optional_settings = optional_settings or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "label": self.label,
            "description": self.description,
            "secret": self.secret,
            "secret_label": self.secret_label,
            "secret_optional": self.secret_optional,
            "meta_fields": [field.to_dict() for field in self.meta_fields],
            "enabled_key": self.enabled_key,
            "testable": self.testable,
            "panel": self.panel,
            "optional_settings": self.optional_settings,
            "settings": [key for key, entry in CATALOG.items() if entry.connection == self.id],
        }


# ---------------------------------------------------------------------------
# Catalog: every key from DEFAULT_PUBLIC_SETTINGS + every SENSITIVE_INTEGRATIONS
# Registration order is display order within a page section.
# ---------------------------------------------------------------------------

CATALOG: dict[str, CatalogEntry] = {}


def _reg(entry: CatalogEntry) -> CatalogEntry:
    CATALOG[entry.key] = entry
    return entry


# ===========================================================================
# You › Profile & monthly plan
# ===========================================================================

_reg(CatalogEntry("discover_investor_horizon", "profile", "Investment horizon",
    "How long you plan to stay invested. Longer horizons carry more equity risk.",
    input_type="select", options=["short", "medium", "long"],
    option_labels={"short": "Short (under 3 years)", "medium": "Medium (3–10 years)", "long": "Long (10+ years)"},
    section="Investor profile"))
_reg(CatalogEntry("discover_investor_risk_appetite", "profile", "Risk appetite",
    "How much volatility and loss you can stomach.",
    input_type="select", options=["low", "medium", "high"],
    option_labels={"low": "Low — conservative", "medium": "Medium", "high": "High — aggressive"},
    section="Investor profile"))
_reg(CatalogEntry("discover_investor_exclusions", "profile", "Exclusions",
    "Sectors, industries or keywords Discover never suggests, comma-separated.",
    placeholder="tobacco, weapons, fossil fuels",
    section="Investor profile"))

_reg(CatalogEntry("monthly_contribution_eur", "profile", "Monthly contribution",
    "What you invest each month. 'This month' splits it across core, factor tilt and stock picks.",
    input_type="number", unit="eur", min=0, section="Monthly plan"))
_reg(CatalogEntry("plan_contribution_mode", "profile", "How it is invested",
    "A standing savings plan buys the core automatically; manual orders lists each order and its fee.",
    input_type="select", options=["savings_plan", "manual_orders"],
    option_labels={"savings_plan": "Savings plan at the broker", "manual_orders": "Manual orders"},
    section="Monthly plan"))
_reg(CatalogEntry("plan_broker", "profile", "Broker for new money",
    "Where each buy in the plan goes, and so its fee: DKB orders from 10 € and savings plans 1,50 €; "
    "Scalable FREE orders 0,99 € and savings plans free. Automatic picks the cheapest synced depot.",
    input_type="select", options=["auto", "dkb", "scalable"],
    option_labels={"auto": "Automatic (cheapest connected broker)", "dkb": "DKB",
                   "scalable": "Scalable Capital (FREE)"},
    extended_help="DKB orders cost 10 € up to 5,000 € (15 € to 20,000 €). Synced Scalable positions count "
    "toward the plan automatically; positions at other brokers once entered under Portfolio → Holdings.",
    section="Monthly plan"))
_reg(CatalogEntry("emergency_reserve_eur", "profile", "Emergency reserve",
    "Cash you keep aside, in euros. Tagesgeld and free broker cash above it is shown as investable; "
    "0 means not set, and the plan then suggests nothing from your cash.",
    input_type="number", unit="eur", min=0, section="Monthly plan",
    extended_help="Your giro account is never counted: that money is for spending. A fixed amount stays "
    "put as balances move, unlike a percentage."))
_reg(CatalogEntry("passive_core_ticker", "profile", "Core ETF ticker",
    "The global equity ETF bought while no strategy has passed the graduation gate.",
    placeholder="EUNL.DE", section="Monthly plan"))
_reg(CatalogEntry("passive_core_isin", "profile", "Core ETF ISIN",
    "Used to match the core ETF against your DKB and broker positions.",
    placeholder="IE00B4L5Y983", section="Monthly plan"))
_reg(CatalogEntry("plan_min_order_eur", "profile", "Minimum stock order",
    "Smaller stock buys go to the core instead, keeping each order fee at or below 1 %.",
    input_type="number", unit="eur", min=0, section="Monthly plan",
    extended_help="DKB's 10 € fee is 1 % of 1,000 €; Scalable's 0,99 € is 1 % of about 100 €."))
_reg(CatalogEntry("plan_tilt_max_pct", "profile", "Factor tilt cap",
    "Largest share of the book a factor-tilt ETF may reach once its backtest passes.",
    input_type="number", unit="pct", min=0, max=100, section="Monthly plan",
    extended_help="15 % keeps tracking error tight (about 1 pp a year worst-case lag); 35 % is moderate."))
_reg(CatalogEntry("plan_satellite_max_pct", "profile", "Stock-pick cap",
    "Largest share of the book single stocks may reach once a strategy passes the evidence gate.",
    input_type="number", unit="pct", min=0, max=100, section="Monthly plan"))
_reg(CatalogEntry("plan_drift_band_pp", "profile", "Drift band",
    "A sleeve is only sold down once it sits this far above target and a year of contributions can't fix it.",
    input_type="number", unit="pp", min=0, section="Monthly plan",
    extended_help="Every sale is a taxable event, so the band is wide on purpose."))
_reg(CatalogEntry("plan_core_isins", "profile", "Extra core ETF ISINs",
    "Broad global equity ETFs to count as core, on top of the built-in MSCI World / ACWI / FTSE All-World list.",
    placeholder="IE00B4L5Y983, IE00BK5BQT80", section="Monthly plan", advanced=True))
_reg(CatalogEntry("plan_tilt_isins", "profile", "Factor tilt ETF ISINs",
    "Factor ETFs held as the tilt sleeve. The monthly tilt money is shared equally across them.",
    placeholder="IE00BP3QZ825", section="Monthly plan", advanced=True))
_reg(CatalogEntry("allocator_universe_isins", "profile", "Allocator candidate ETFs",
    "ETFs the Quant Lab allocator may split new money across (comma-separated ISINs). Empty uses MSCI World + Emerging Markets IMI.",
    placeholder="IE00B4L5Y983, IE00BK5BQT80, IE00BKM4GZ66", section="Monthly plan", advanced=True))

_reg(CatalogEntry("mc_cma_real_return", "profile", "Expected real return",
    "Long-run return a year after inflation, compounded, that the Monte Carlo projection grows the book at. "
    "From a published capital-market assumption, never from your own few years of history.",
    input_type="number", unit="fraction_pct", min=-0.05, max=0.15, section="Projections",
    extended_help="Two to ten years of returns pin the average down only to within about ±10 % a year, so a "
    "published long-run estimate is used instead. AQR's 2026 figure for global developed equities is 4.2 %; "
    "the projection also shows how the chance of reaching a goal moves one standard error either side."))
_reg(CatalogEntry("mc_cma_source", "profile", "Source of the expected return",
    "Who published the figure above, so you can check and update it.", section="Projections", advanced=True))
_reg(CatalogEntry("mc_cma_as_of", "profile", "Expected return as of",
    "The date the published figure refers to.", input_type="date", section="Projections", advanced=True))
_reg(CatalogEntry("mc_goal_eur", "profile", "Wealth goal",
    "A target in today's euros; the projection shows the chance of reaching it.",
    input_type="number", unit="eur", min=0, section="Projections"))
_reg(CatalogEntry("benchmark_ticker", "profile", "Benchmark",
    "What the book's return, beta and alpha are measured against. Its prices are converted to EUR.",
    placeholder="EUNL.DE", section="Display",
    extended_help="The default, EUNL.DE, is MSCI World in EUR, unhedged, with dividends reinvested: the "
    "market you would hold if you did nothing clever. A benchmark should match what you hold and be in "
    "your currency; SPY is neither."))
_reg(CatalogEntry("currency", "profile", "Reporting currency",
    "Currency every amount in the app is shown in.",
    input_type="select", options=["EUR", "USD", "GBP", "CHF"], section="Display"))

# ===========================================================================
# You › Tax
# ===========================================================================

_reg(CatalogEntry("tax_residency_country", "tax", "Tax residence",
    "Picks the estimation engine. Germany enables the KAP estimates, the Netherlands Box 3.",
    input_type="select", options=["DE", "NL", "OTHER"],
    option_labels={"DE": "Germany", "NL": "Netherlands", "OTHER": "Other (no estimates)"},
    section="Tax residence"))
_reg(CatalogEntry("tax_estimation_enabled", "tax", "Show tax estimates",
    "Kapitalertragsteuer, Soli, Kirchensteuer and Vorabpauschale estimates in the Tax Cockpit.",
    input_type="boolean", section="Tax residence"))
_reg(CatalogEntry("church_tax", "tax", "Church tax",
    "8 % in Bavaria and Baden-Württemberg, 9 % in every other state.",
    input_type="select", options=["none", "0.08", "0.09"],
    option_labels={"none": "None", "0.08": "8 % (BY, BW)", "0.09": "9 % (other states)"},
    section="Tax residence"))

_reg(CatalogEntry("freistellungsauftrag_amount", "tax", "Sparer-Pauschbetrag",
    "The statutory yearly allowance: 1,000 € single, 2,000 € married filing jointly. Only change it if the "
    "law changes before the app does.",
    input_type="number", unit="eur", min=0, section="Allowance", advanced=True))
_reg(CatalogEntry("freistellungsauftrag_used", "tax", "Allowance already used at other banks",
    "Income this year already set against orders at banks other than DKB and Scalable. Usually leave it "
    "empty and enter those orders below; the larger of the two is used.",
    input_type="number", unit="eur", min=0, section="Allowance", advanced=True))
_reg(CatalogEntry("freistellungsauftrag_other_banks_eur", "tax", "Freistellungsaufträge at other banks",
    "Total of the orders on file at banks and brokers other than DKB and Scalable Capital. All orders "
    "together may not exceed the Sparer-Pauschbetrag, so this is the part DKB and Scalable can't get. "
    "Empty = none (the allowance used elsewhere counts as the minimum).",
    input_type="number", unit="eur", min=0, section="Allowance"))
_reg(CatalogEntry("freistellungsauftrag_dkb_eur", "tax", "Freistellungsauftrag at DKB",
    "The amount on file at DKB (Banking › Mein Profil › Steuerliche Angaben). One order covers "
    "the Girokonto, Tagesgeld and depot.",
    input_type="number", unit="eur", min=0, section="Allowance"))
_reg(CatalogEntry("freistellungsauftrag_scalable_eur", "tax", "Freistellungsauftrag at Scalable Capital",
    "The amount on file at Scalable (app: Profil › Freistellungsauftrag). One order covers the depot "
    "and the Tagesgeld/overnight account.",
    input_type="number", unit="eur", min=0, section="Allowance"))
_reg(CatalogEntry("tax_interest_rate_dkb", "tax", "DKB Tagesgeld interest rate",
    "Used to project this year's DKB interest against the Freistellungsauftrag. Scalable's rate comes from the sync.",
    input_type="number", unit="fraction_pct", min=0, max=0.2, section="Allowance", advanced=True))
_reg(CatalogEntry("tax_spouse_allowance", "tax", "Married, filing jointly",
    "Share the 2,000 € joint allowance instead of 1,000 €.",
    input_type="boolean", section="Allowance"))

_reg(CatalogEntry("tax_nv_certificate", "tax", "NV certificate filed",
    "Banks withhold no tax; the cockpit suggests realising gains tax-free up to the "
    "Grundfreibetrag plus the allowance.",
    input_type="boolean", section="NV certificate",
    extended_help="Nichtveranlagungsbescheinigung — typically for students without other income."))
_reg(CatalogEntry("tax_nv_valid_until", "tax", "NV certificate valid until",
    "End date printed on the certificate, always a 31 December (at most three years). The cockpit warns "
    "60 days before.",
    input_type="date", section="NV certificate"))
_reg(CatalogEntry("tax_nv_filed_dkb", "tax", "NV certificate filed at DKB",
    "DKB has a copy (upload under Mein Profil › Steuerliche Angaben). Without it DKB withholds tax as usual.",
    input_type="boolean", section="NV certificate"))
_reg(CatalogEntry("tax_nv_filed_scalable", "tax", "NV certificate filed at Scalable Capital",
    "Scalable has a copy (upload link from support, or post; by 15 December). Without it Scalable withholds "
    "tax above its Freistellungsauftrag.",
    input_type="boolean", section="NV certificate"))
_reg(CatalogEntry("tax_other_income_eur", "tax", "Other income this year",
    "Salary and other non-capital income after Werbungskosten; it uses up the Grundfreibetrag first.",
    input_type="number", unit="eur", min=0, section="Your situation"))
_reg(CatalogEntry("tax_health_insurance", "tax", "Health insurance",
    "German family insurance has an income limit that caps tax-free harvesting; co-insurance abroad is "
    "held to the same limit to be safe.",
    input_type="select", options=["unknown", "de_family", "de_own", "foreign"],
    option_labels={"unknown": "Not set", "de_family": "German family insurance",
                   "de_own": "Own German insurance", "foreign": "Insured abroad (e.g. Austrian ÖGK)"},
    section="Your situation"))
_reg(CatalogEntry("tax_bafoeg", "tax", "Receiving BAföG",
    "BAföG counts capital income and assets; the harvest suggestion warns when this is on.",
    input_type="boolean", section="Your situation"))

# ===========================================================================
# Connections › Banks & brokers (DKB, Scalable Capital)
# ===========================================================================

_reg(CatalogEntry("dkb", "banking", "DKB online-banking PIN",
    "Read-only FinTS access. Needs your DKB login name (not your e-mail) and PIN; "
    "every sync is approved with a push TAN in the DKB app.",
    input_type="password", sensitive=True, connection="dkb",
    extended_help="No payment initiation is possible. chipTAN, photoTAN and smsTAN are not supported — "
    "only the DKB-App push TAN. Run the self-test before a full sync.",
    setup_steps=["Enter your DKB login name (not your e-mail)", "Enter your online-banking PIN (encrypted at rest)",
                 "Save, then run the self-test", "Start a sync from Portfolio → Accounts"]))
_reg(CatalogEntry("dkb_push_timeout_seconds", "banking", "Push-TAN timeout",
    "How long a sync waits for you to approve it in the DKB app.",
    input_type="number", unit="seconds", min=10, section="Sync", advanced=True))
_reg(CatalogEntry("dkb_poll_interval_seconds", "banking", "Poll interval",
    "How often a waiting sync asks DKB whether the push TAN was approved.",
    input_type="number", unit="seconds", min=1, section="Sync", advanced=True))
_reg(CatalogEntry("dkb_debug_fints_logging", "banking", "Log raw FinTS messages",
    "Captures the FinTS wire protocol in self-test results and sync logs. Turn off after troubleshooting.",
    input_type="boolean", section="Sync", advanced=True))
_reg(CatalogEntry("dkb_fints_url", "banking", "FinTS endpoint",
    "Only change this if DKB moves its FinTS gateway.",
    placeholder="https://fints.dkb.de/fints", section="FinTS protocol", advanced=True))
_reg(CatalogEntry("dkb_blz", "banking", "Bankleitzahl",
    "DKB's bank code.", placeholder="12030000", section="FinTS protocol", advanced=True))
_reg(CatalogEntry("dkb_provider", "banking", "Adapter",
    "FinTS is the only DKB adapter.",
    input_type="select", options=["fints"], option_labels={"fints": "FinTS (python-fints)"},
    section="FinTS protocol", advanced=True))
_reg(CatalogEntry("dkb_product_id", "banking", "FinTS product ID",
    "Leave empty — DKB doesn't check it and the built-in ID works for everyone.",
    section="FinTS protocol", advanced=True))
_reg(CatalogEntry("dkb_tan_security_function", "banking", "TAN security function",
    "Leave empty to detect it automatically.", section="FinTS protocol", advanced=True))
_reg(CatalogEntry("dkb_tan_medium", "banking", "TAN medium",
    "Leave empty to detect it automatically.", section="FinTS protocol", advanced=True))

_reg(CatalogEntry("scalable_enabled", "banking", "Sync Scalable Capital",
    "Read-only sync of holdings, cash, transactions and savings plans through Scalable's official sc CLI. "
    "It can never trade: the CLI session is read-only and every order is refused locally.",
    input_type="boolean", connection="scalable", section="Scalable Capital",
    doc_url="https://github.com/ScalableCapital/scalable-cli"))
_reg(CatalogEntry("scalable_portfolio_id", "banking", "Scalable portfolio ID",
    "The broker portfolio to sync. Leave empty to use the one selected in the CLI's broker context.",
    connection="scalable", section="Scalable Capital", placeholder="From: sc broker context show"))
_reg(CatalogEntry("scalable_sync_hours", "banking", "Sync every",
    "How often the worker syncs Scalable. No TAN is needed, unlike DKB.",
    input_type="number", unit="hours", min=1, max=48, connection="scalable", section="Scalable Capital"))
_reg(CatalogEntry("scalable_cli_user", "banking", "CLI service user",
    "The Unix user that owns the Scalable session; Quantfolio runs the CLI as this user through sudo. "
    "Empty runs the wrapper directly (development only).",
    connection="scalable", section="Scalable CLI", advanced=True, placeholder="scalable-cli-user"))
_reg(CatalogEntry("scalable_wrapper_path", "banking", "Allow-list wrapper",
    "The root-owned wrapper that only lets read commands through to sc.",
    connection="scalable", section="Scalable CLI", advanced=True, placeholder="/usr/local/libexec/quantfolio-sc-ro"))
_reg(CatalogEntry("scalable_binary_sha256", "banking", "Pinned sc SHA-256",
    "When set, Quantfolio refuses to run /usr/local/bin/sc (the only binary the wrapper starts) if its hash "
    "differs. Paste the hash of the release you verified.",
    connection="scalable", section="Scalable CLI", advanced=True))
_reg(CatalogEntry("scalable_timeout_seconds", "banking", "CLI timeout",
    "How long one sc command may run before it is stopped.",
    input_type="number", unit="seconds", min=5, max=600, connection="scalable", section="Scalable CLI", advanced=True))
_reg(CatalogEntry("scalable_require_read_only_guard", "banking", "Require the trade guard",
    "Sync only when sc reports that its local trade controls refuse every order (allowed_isins = []). "
    "Turn off only for a one-off test.",
    input_type="boolean", connection="scalable", section="Scalable CLI", advanced=True))

# ===========================================================================
# Connections › Market data
# ===========================================================================

_reg(CatalogEntry("provider_chain_json", "market_data", "Provider order",
    "Providers are asked in this order; the first one that returns data wins.",
    input_type="provider_chain", section="Priority"))
_reg(CatalogEntry("finnhub", "market_data", "Finnhub API key",
    "Quotes, fundamentals and news. Free tier: 60 requests a minute.",
    doc_url="https://finnhub.io/register", input_type="password", sensitive=True, connection="finnhub",
    setup_steps=["Register at finnhub.io", "Copy the API key from the dashboard", "Paste it here and test"]))
_reg(CatalogEntry("tiingo", "market_data", "Tiingo API token",
    "Split- and dividend-adjusted US price history going back 50+ years.",
    doc_url="https://www.tiingo.com/account/api/token", input_type="password", sensitive=True, connection="tiingo",
    setup_steps=["Sign up at tiingo.com", "Copy the API token", "Paste it here and test"]))
_reg(CatalogEntry("twelvedata", "market_data", "Twelve Data API key",
    "Global coverage across 100+ exchanges. Free tier: 800 requests a day, 8 a minute.",
    doc_url="https://twelvedata.com/apikey", input_type="password", sensitive=True, connection="twelvedata",
    setup_steps=["Sign up at twelvedata.com/apikey", "Copy the API key", "Paste it here and test"]))
_reg(CatalogEntry("alpaca", "market_data", "Alpaca API key",
    "US equities via the Alpaca Data API v2. Needs both the API key and the secret key.",
    doc_url="https://alpaca.markets/docs/api-references/market-data/",
    input_type="password", sensitive=True, connection="alpaca",
    setup_steps=["Create a free account at alpaca.markets", "Generate an API key and secret",
                 "Paste both here and test"]))
_reg(CatalogEntry("databento", "market_data", "Databento API key",
    "Tick-level US equities history. Free tier: 250,000 messages a month.",
    doc_url="https://databento.com/signup", input_type="password", sensitive=True, connection="databento"))
_reg(CatalogEntry("alphavantage", "market_data", "Alpha Vantage API key",
    "Stock and ETF prices. Free tier: 25 requests a day.",
    doc_url="https://www.alphavantage.co/support/#api-key", input_type="password", sensitive=True,
    connection="alphavantage"))
_reg(CatalogEntry("fred", "market_data", "FRED API key",
    "Federal Reserve macro series (rates, spreads, employment) used by the regime model.",
    doc_url="https://fred.stlouisfed.org/docs/api/api_key.html", input_type="password", sensitive=True,
    connection="fred"))
_reg(CatalogEntry("eod", "market_data", "EODHD API key",
    "End-of-day prices for 60+ exchanges. Free tier: 20 calls a day.",
    doc_url="https://eodhd.com/register", input_type="password", sensitive=True, connection="eod"))
_reg(CatalogEntry("massive", "market_data", "Massive (Polygon.io) API key",
    "Aggregated market data from Massive.com, the renamed Polygon.io.",
    doc_url="https://polygon.io/docs", input_type="password", sensitive=True, connection="massive"))
_reg(CatalogEntry("openbb_enabled", "market_data", "Use OpenBB",
    "Route requests through your local OpenBB Platform server.",
    input_type="boolean", connection="openbb"))
_reg(CatalogEntry("openbb_api_url", "market_data", "Server URL",
    "Address of the OpenBB API server.", placeholder="http://127.0.0.1:6900", connection="openbb"))
_reg(CatalogEntry("openbb_default_provider", "market_data", "Default data source",
    "OpenBB extension used for equity requests. It must be installed on the OpenBB server.",
    placeholder="yfinance", connection="openbb", doc_url="https://docs.openbb.co/platform",
    extended_help="Keyless: cboe, finviz, yfinance, sec, famafrench, ecb, imf, federal_reserve, oecd, tmx. "
    "With a key (set on the OpenBB server): fmp, polygon, tiingo, fred, tradier, alpha_vantage, intrinio. "
    "Install them with backend/scripts/setup_openbb_providers.sh on the OpenBB LXC."))
_reg(CatalogEntry("sec_edgar_user_agent", "market_data", "Contact name and e-mail",
    "Sent to sec.gov by the daily insider-trading refresh, as SEC's fair-access policy asks.",
    placeholder="Jane Doe jane@example.com", connection="sec_edgar",
    doc_url="https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data"))

_reg(CatalogEntry("rss_feed_urls", "market_data", "News feeds",
    "RSS or Atom feed URLs shown on the News page, comma-separated. Empty uses the built-in feeds.",
    placeholder="https://www.ecb.europa.eu/rss/press.html, …", section="News"))

_reg(CatalogEntry("isin_ticker_overrides", "market_data", "ISIN → ticker overrides",
    "Fixes an ISIN that resolves to the wrong ticker, e.g. {\"IE00B4L5Y983\": \"EUNL.DE\"}.",
    input_type="json", section="Symbol mapping", advanced=True,
    extended_help="Consulted after automatic resolution (yfinance, justETF). Applied across Quant Lab, "
    "backtests and price backfill. Include the exchange suffix (.DE, .AS, .L)."))
_reg(CatalogEntry("etf_index_map", "market_data", "ETF → index map",
    "Which index's currency mix an ETF holds, e.g. {\"IE00BYZK4552\": \"msci_world\"}. "
    "Adds to the ~160 built-in index ETFs.",
    input_type="json", section="Symbol mapping", advanced=True,
    extended_help="Used by the risk report and FX stress tests to look through an index ETF. Known keys: "
    "msci_world, msci_acwi, msci_acwi_imi, msci_em, msci_europe, stoxx_europe_600, ftse_all_world, sp500, "
    "nasdaq_100, euro_stoxx_50, dax, and hedged:EUR (or another currency) for a hedged share class."))
_reg(CatalogEntry("history_stale_days", "market_data", "Price history counts as stale after",
    "A series whose newest bar is older than this is flagged in Discover backtests (1–30).",
    input_type="number", unit="days", min=1, max=30, section="Data quality", advanced=True))

# ===========================================================================
# Connections › AI models
# ===========================================================================

_reg(CatalogEntry("llm_base_url", "ai", "Server URL",
    "OpenAI-compatible endpoint of your llama.cpp, Ollama or vLLM server.",
    placeholder="http://127.0.0.1:8080/v1", connection="llm"))
_reg(CatalogEntry("llm_model", "ai", "Model",
    "Must match a model loaded on the server.", placeholder="qwen3.5-9b", connection="llm"))
_reg(CatalogEntry("llm", "ai", "Local LLM API key",
    "Only needed if your server requires authentication — llama.cpp and Ollama usually don't.",
    input_type="password", sensitive=True, connection="llm"))
_reg(CatalogEntry("anthropic_model", "ai", "Model",
    "Claude model used for cloud fallback.", placeholder="claude-opus-4-8",
    doc_url="https://docs.anthropic.com/en/docs/about-claude/models",
    status="needs_worker_restart", connection="anthropic"))
_reg(CatalogEntry("anthropic", "ai", "Anthropic API key",
    "Starts with 'sk-ant-'. Only used when cloud fallback is on.",
    doc_url="https://console.anthropic.com/settings/keys", input_type="password", sensitive=True,
    connection="anthropic"))
_reg(CatalogEntry("openai_model", "ai", "Model",
    "GPT model used for cloud fallback.", placeholder="gpt-4o",
    doc_url="https://platform.openai.com/docs/models",
    status="needs_worker_restart", connection="openai"))
_reg(CatalogEntry("openai", "ai", "OpenAI API key",
    "Starts with 'sk-proj-'. Only used when cloud fallback is on.",
    doc_url="https://platform.openai.com/api-keys", input_type="password", sensitive=True,
    connection="openai"))
_reg(CatalogEntry("llm_cloud_fallback_enabled", "ai", "Allow cloud fallback",
    "Batch research may use Claude or GPT when a key is set. Chat and routine tasks always stay local.",
    input_type="boolean", section="Cloud fallback"))
_reg(CatalogEntry("prompt_raw_retention_hours", "ai", "Keep raw prompts for",
    "Unredacted prompt and response pairs are kept this long for debugging, then redacted.",
    input_type="number", unit="hours", min=1, status="needs_worker_restart",
    section="Prompt log", advanced=True))
_reg(CatalogEntry("prompt_delete_after_days", "ai", "Delete prompt log after",
    "Audit events older than this are deleted permanently.",
    input_type="number", unit="days", min=1, status="needs_worker_restart",
    section="Prompt log", advanced=True))

# ===========================================================================
# Connections › Notes & alerts
# ===========================================================================

_reg(CatalogEntry("obsidian_enabled", "notes_alerts", "Use Obsidian",
    "Save research notes and snapshots to your vault.", input_type="boolean", connection="obsidian"))
_reg(CatalogEntry("obsidian_rest_url", "notes_alerts", "REST API URL",
    "Default ports: 27123 (HTTP) or 27124 (HTTPS).", placeholder="http://127.0.0.1:27123",
    connection="obsidian"))
_reg(CatalogEntry("obsidian", "notes_alerts", "Bearer token",
    "The token you set in the Local REST API plugin.",
    doc_url="https://github.com/coddingtonbear/obsidian-local-rest-api",
    input_type="password", sensitive=True, connection="obsidian",
    setup_steps=["In Obsidian, install the community plugin 'Local REST API'", "Enable it and set a bearer token",
                 "Paste the token and the REST URL here, then test"]))
_reg(CatalogEntry("telegram", "notes_alerts", "Bot token",
    "Log expenses by message, ask /balance and /summary, and receive budget alerts.",
    doc_url="https://core.telegram.org/bots#how-do-i-create-a-bot",
    input_type="password", sensitive=True, connection="telegram",
    setup_steps=["Message @BotFather and run /newbot", "Paste the bot token here and save",
                 "Generate a pairing code and send '/start CODE' to your bot"]))
_reg(CatalogEntry("telegram_mode", "notes_alerts", "How the bot receives messages",
    "Polling needs no public address: the worker asks Telegram for new messages. "
    "Webhook has Telegram call this server, which must be reachable over HTTPS.",
    input_type="select", options=["polling", "webhook"],
    option_labels={"polling": "Polling (recommended)", "webhook": "Webhook"},
    connection="telegram"))
_reg(CatalogEntry("smtp_host", "notes_alerts", "Server", "SMTP host name.",
    placeholder="smtp.gmail.com", connection="smtp"))
_reg(CatalogEntry("smtp_port", "notes_alerts", "Port", "587 for STARTTLS, 465 for SSL.",
    input_type="number", min=1, max=65535, connection="smtp"))
_reg(CatalogEntry("smtp_use_tls", "notes_alerts", "Use STARTTLS", "On for port 587, off for SSL on 465.",
    input_type="boolean", connection="smtp"))
_reg(CatalogEntry("smtp_username", "notes_alerts", "Username", "Usually your e-mail address.",
    connection="smtp"))
_reg(CatalogEntry("smtp_from_address", "notes_alerts", "From address", "Shown as the sender.",
    placeholder="noreply@quantfolio.local", connection="smtp"))
_reg(CatalogEntry("smtp_allow_plaintext", "notes_alerts", "Allow unencrypted SMTP",
    "Off: mail is only sent over STARTTLS or SSL. Turn on only for a trusted local relay.",
    input_type="boolean", connection="smtp", advanced=True))
_reg(CatalogEntry("smtp", "notes_alerts", "Password",
    "Use an app-specific password for Gmail or iCloud, not your account password.",
    doc_url="https://support.google.com/accounts/answer/185833",
    input_type="password", sensitive=True, connection="smtp"))

# ===========================================================================
# Engine tuning › Discover & decision loop
# ===========================================================================

_reg(CatalogEntry("decision_loop_mode", "discover", "Real-money guidance",
    "While no strategy clears the graduation gate, guidance stays on the passive core ETF. "
    "A manual switch on top of the statistical gate.",
    input_type="select", options=["passive_core", "live"],
    option_labels={"passive_core": "Passive core only", "live": "Live (graduated strategies)"},
    section="Decision loop",
    extended_help="The gate has five conditions: Deflated Sharpe, Newey-West t-stat, Probability of "
    "Backtest Overfitting, Minimum Track Record Length and out-of-sample confirmation. "
    "Set 'Live' only once a champion has actually graduated."))
_reg(CatalogEntry("scheduled_recommendation_refresh", "discover", "Weekly recommendation refresh",
    "Sunday job that regenerates the deterministic recommendations.",
    input_type="boolean", status="needs_worker_restart", section="Decision loop"))
_reg(CatalogEntry("er_mode", "discover", "Expected-return method",
    "Where dossier return anchors come from. Only 'Trailing' is the established default.",
    input_type="select", options=["trailing", "blocks", "bl"],
    option_labels={"trailing": "Trailing returns (default)", "blocks": "Building blocks",
                   "bl": "Black-Litterman"},
    status="experimental", section="Candidate scoring"))
_reg(CatalogEntry("discover_recency_penalty_halflife_days", "discover", "Repeat-suggestion penalty half-life",
    "Stocks suggested recently score lower, decaying with this half-life, so the list doesn't repeat itself.",
    input_type="number", unit="days", min=1, section="Candidate scoring",
    extended_help="ETFs and candidates above the BUY threshold are always exempt."))

# ===========================================================================
# Engine tuning › Market regime
# ===========================================================================

_reg(CatalogEntry("regime_model_kind", "regime", "Model",
    "The jump model is the live classifier; the HMM stays fitted as its fallback.",
    input_type="select", options=["jump", "hmm"],
    option_labels={"jump": "Statistical jump model (default)", "hmm": "Hidden Markov model"},
    section="Model"))
_reg(CatalogEntry("regime_index_symbol", "regime", "Index",
    "Market index the regime is read from.", placeholder="^STOXX50E", section="Model"))
_reg(CatalogEntry("regime_jump_penalty", "regime", "Jump penalty",
    "Higher values make the jump model switch regimes less often. Applies at the next weekly refit.",
    input_type="number", min=0, section="Model"))
_reg(CatalogEntry("regime_jump_model_id", "regime", "Jump model artefact",
    "Name the fitted jump model is saved under.", section="Model", advanced=True))
_reg(CatalogEntry("regime_model_id", "regime", "HMM fallback artefact",
    "Name the fitted HMM is saved under. Used when the jump model is unavailable.",
    section="Model", advanced=True))
_reg(CatalogEntry("regime_crisis_vix_threshold", "regime", "VIX above",
    "Flags a crisis regime when the VIX closes above this level.",
    input_type="number", unit="points", min=0, section="Crisis override"))
_reg(CatalogEntry("regime_crisis_credit_spread_threshold", "regime", "Credit spread above",
    "Flags a crisis regime when the high-yield credit spread exceeds this.",
    input_type="number", unit="pp", min=0, section="Crisis override"))
_reg(CatalogEntry("regime_crisis_drawdown_threshold", "regime", "Index drawdown below",
    "Flags a crisis regime when the index is this far below its peak.",
    input_type="number", unit="fraction_pct", max=0, section="Crisis override"))
_reg(CatalogEntry("regime_max_bar_age_days", "regime", "Newest index bar at most",
    "Older data triggers one backfill; if it stays stale, classification is skipped rather than guessed.",
    input_type="number", unit="days", min=1, section="Freshness", advanced=True))
_reg(CatalogEntry("regime_stale_ttl_hours", "regime", "Snapshot counts as stale after",
    "The regime chip is flagged stale once the last snapshot is this old.",
    input_type="number", unit="hours", min=1, section="Freshness", advanced=True))

# ===========================================================================
# Engine tuning › AlphaCrafter & Quant Lab
# ===========================================================================

_reg(CatalogEntry("alphacrafter_index_basket", "research", "Validation universe",
    "Tickers factors are validated across, comma-separated. Empty uses Euro Stoxx 50 + S&P 100 "
    "(150 names); fewer than 30 names is ignored.",
    placeholder="Built-in: Euro Stoxx 50 + S&P 100", section="Factor mining"))
_reg(CatalogEntry("alphacrafter_ic_horizon_days", "research", "IC horizon",
    "Forward-return horizon the information coefficient is measured over.",
    input_type="number", unit="days", min=1, section="Factor mining"))
_reg(CatalogEntry("alphacrafter_min_ic", "research", "Minimum IC",
    "A factor needs at least this information coefficient to be accepted.",
    input_type="number", min=0, section="Factor mining"))
_reg(CatalogEntry("alphacrafter_min_icir", "research", "Minimum ICIR",
    "…and at least this IC information ratio (mean IC ÷ its volatility).",
    input_type="number", min=0, section="Factor mining"))
_reg(CatalogEntry("alphacrafter_propose_llm", "research", "Let the LLM propose factors",
    "Adds LLM-written factor ideas to the mining queue.", input_type="boolean", section="Factor mining"))
_reg(CatalogEntry("alphacrafter_rebalance_freqs", "research", "Rebalance frequencies",
    "Frequencies the trader sweep tries: D (daily), W (weekly), M (monthly), comma-separated.",
    placeholder="W,M", section="Trader sweep",
    extended_help="Each value multiplies the sweep grid. Malformed values fall back to 'W,M' with a warning."))
_reg(CatalogEntry("alphacrafter_position_sizes", "research", "Position sizes",
    "Target weights per held name, as fractions, comma-separated (0.1 = 10 %).",
    placeholder="0.05,0.1,0.2", section="Trader sweep",
    extended_help="Weights are capped at run time so gross exposure never exceeds 100 %."))
_reg(CatalogEntry("alphacrafter_commissions", "research", "Commission rates",
    "Per-trade cost as a fraction of turnover, comma-separated (0.001 = 0.1 %).",
    placeholder="0.001", section="Trader sweep"))
_reg(CatalogEntry("alphacrafter_max_positions", "research", "Maximum positions",
    "Number of top-scored names the trader holds at each rebalance.",
    input_type="number", min=1, section="Trader sweep"))
_reg(CatalogEntry("quant_lab_min_ic", "research", "Minimum out-of-sample IC",
    "Signal-batch factors need at least this out-of-sample IC to survive.",
    input_type="number", min=0, section="Quant Lab signal gate"))
_reg(CatalogEntry("quant_lab_min_icir", "research", "Minimum out-of-sample ICIR",
    "…and at least this out-of-sample IC information ratio.",
    input_type="number", min=0, section="Quant Lab signal gate"))
_reg(CatalogEntry("scheduled_quant_experiment_refresh", "research", "Weekly experiment run",
    "Sunday job that re-runs every active quant experiment.",
    input_type="boolean", status="needs_worker_restart", section="Quant Lab signal gate"))

# ===========================================================================
# Engine tuning › Risk alerts
# ===========================================================================

_reg(CatalogEntry("verification_drawdown_warning", "verification", "Drawdown warning at",
    "Warn when your current holdings are this far below their one-year peak.",
    input_type="number", unit="fraction_pct", max=0, section="Drawdown"))
_reg(CatalogEntry("verification_drawdown_critical", "verification", "Drawdown critical at",
    "Critical alert when your current holdings are this far below their one-year peak.",
    input_type="number", unit="fraction_pct", max=0, section="Drawdown"))
_reg(CatalogEntry("verification_concentration_single", "verification", "Largest holding above",
    "Alert when one single stock is at least this share of the portfolio (ETFs and funds are not single names).",
    input_type="number", unit="fraction_pct", min=0, max=1, section="Concentration"))
_reg(CatalogEntry("verification_concentration_top3", "verification", "Top three holdings above",
    "Alert when the three largest single stocks together reach this share.",
    input_type="number", unit="fraction_pct", min=0, max=1, section="Concentration"))

# ===========================================================================
# System › Security & access
# ===========================================================================

_reg(CatalogEntry("webauthn_rp_id", "security", "Passkey domain",
    "The domain passkeys are bound to (e.g. app.example.com, or 'localhost'). Never a bare IP. "
    "Changing it invalidates every registered passkey.",
    doc_url="https://www.w3.org/TR/webauthn-2/#rp-id", section="Passkeys", advanced=True))
_reg(CatalogEntry("webauthn_rp_name", "security", "Passkey display name",
    "The app name your browser shows when you register or use a passkey — a name, not a URL.",
    placeholder="QuantFolio", section="Passkeys"))
_reg(CatalogEntry("frontend_origin", "security", "Allowed web origins",
    "Comma-separated origins (scheme + host + port) the browser may call the API from.",
    placeholder="http://localhost:5173, https://app.example.com",
    doc_url="https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS", section="Network", advanced=True))

# ===========================================================================
# System › Storage & performance
# ===========================================================================

_reg(CatalogEntry("parquet_panel_dir", "system", "Point-in-time panel",
    "Directory of the PIT Parquet panel Quant Lab reads.", placeholder="/tmp/quantfolio_parquet_panel",
    status="needs_worker_restart", section="Storage"))
_reg(CatalogEntry("quant_lab_signal_batch_dir", "system", "Signal-batch output",
    "Where the Quant Lab signal batch writes its Parquet results.", placeholder="/tmp/quantfolio_signal_batch",
    status="needs_worker_restart", section="Storage"))
_reg(CatalogEntry("quant_factors_ff_cache_dir", "system", "Fama-French cache",
    "Cache for downloaded Fama-French factor files.", placeholder="/tmp/quantfolio_ff_cache",
    status="needs_worker_restart", section="Storage"))
_reg(CatalogEntry("duckdb_memory_limit", "system", "DuckDB memory limit",
    "Memory cap for PIT panel reads, e.g. 2GB. The env var QUANTFOLIO_DUCKDB_MEMORY_LIMIT wins.",
    placeholder="2GB", status="needs_worker_restart", section="DuckDB"))
_reg(CatalogEntry("duckdb_threads", "system", "DuckDB threads",
    "Threads for PIT panel reads. The env var QUANTFOLIO_DUCKDB_THREADS wins.",
    input_type="number", min=1, status="needs_worker_restart", section="DuckDB"))
_reg(CatalogEntry("duckdb_temp_directory", "system", "DuckDB spill directory",
    "Where DuckDB spills to disk. Empty uses DuckDB's default; QUANTFOLIO_DUCKDB_TMPDIR wins.",
    status="needs_worker_restart", section="DuckDB", advanced=True))


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

CONNECTIONS: dict[str, Connection] = {}


def _conn(connection: Connection) -> Connection:
    CONNECTIONS[connection.id] = connection
    return connection


_conn(Connection("dkb", "banking", "DKB (FinTS)", "Read-only account and depot sync.",
    secret="dkb", secret_label="Online-banking PIN",
    meta_fields=[MetaField("username", "DKB login name", placeholder="Your login name, not your e-mail")]))

_conn(Connection("scalable", "banking", "Scalable Capital (sc CLI)",
    "Read-only depot, cash and transaction sync. Log in once on the server; no credential is stored here.",
    enabled_key="scalable_enabled", panel="scalable_login",
    optional_settings=["scalable_portfolio_id", "scalable_binary_sha256", "scalable_cli_user"]))

_conn(Connection("finnhub", "market_data", "Finnhub", "Quotes, fundamentals and news.", secret="finnhub"))
_conn(Connection("tiingo", "market_data", "Tiingo", "Adjusted US price history.", secret="tiingo",
    secret_label="API token"))
_conn(Connection("twelvedata", "market_data", "Twelve Data", "Global prices across 100+ exchanges.",
    secret="twelvedata"))
_conn(Connection("alpaca", "market_data", "Alpaca Markets", "US equities data.", secret="alpaca",
    meta_fields=[MetaField("api_secret", "Secret key", sensitive=True)]))
_conn(Connection("databento", "market_data", "Databento", "Tick-level US equities history.",
    secret="databento"))
_conn(Connection("alphavantage", "market_data", "Alpha Vantage", "Stock and ETF prices.",
    secret="alphavantage"))
_conn(Connection("fred", "market_data", "FRED", "US macro and rates series.", secret="fred"))
_conn(Connection("eod", "market_data", "EODHD", "End-of-day prices, 60+ exchanges.", secret="eod"))
_conn(Connection("massive", "market_data", "Massive (Polygon.io)", "Aggregated market data.",
    secret="massive"))
_conn(Connection("openbb", "market_data", "OpenBB Platform", "Self-hosted data gateway.",
    enabled_key="openbb_enabled"))
_conn(Connection("sec_edgar", "market_data", "SEC EDGAR", "Insider filings (Form 4). No key, but a contact.",
    testable=False))

_conn(Connection("llm", "ai", "Local LLM", "Runs research, dossiers and chat.",
    secret="llm", secret_optional=True))
_conn(Connection("anthropic", "ai", "Anthropic Claude", "Optional cloud fallback.", secret="anthropic"))
_conn(Connection("openai", "ai", "OpenAI", "Optional cloud fallback.", secret="openai"))

_conn(Connection("obsidian", "notes_alerts", "Obsidian", "Research notes in your vault.",
    secret="obsidian", secret_label="Bearer token", enabled_key="obsidian_enabled"))
_conn(Connection("telegram", "notes_alerts", "Telegram bot", "Expense logging and budget alerts.",
    secret="telegram", secret_label="Bot token", panel="telegram_pairing"))
_conn(Connection("smtp", "notes_alerts", "E-mail (SMTP)", "Alert and digest e-mails.",
    secret="smtp", secret_label="Password"))


# Meta keys stored inside the encrypted credential although the drawer shows them
# as plain fields (DKB login name) or not at all (the generated Telegram webhook
# secret). Same packing as a ``sensitive`` MetaField; only the UI treatment differs.
ENCRYPTED_META_KEYS: dict[str, frozenset[str]] = {
    "dkb": frozenset({"username"}),
    "telegram": frozenset({"webhook_secret"}),
}


def sensitive_meta_keys(service: str) -> set[str]:
    """Meta keys of *service* that must be stored encrypted, never in meta_json."""
    keys = set(ENCRYPTED_META_KEYS.get(service, ()))
    connection = CONNECTIONS.get(service)
    if connection is not None:
        keys |= {field.key for field in connection.meta_fields if field.sensitive}
    return keys


def get_catalog() -> dict[str, dict[str, Any]]:
    """Return the full catalog as a plain dict keyed by catalog entry key."""
    return {key: entry.to_dict() for key, entry in CATALOG.items()}


def get_catalog_for_group(group: str) -> dict[str, dict[str, Any]]:
    """Return catalog entries for a specific group."""
    return {
        key: entry.to_dict()
        for key, entry in CATALOG.items()
        if entry.group == group
    }


def get_connections() -> list[dict[str, Any]]:
    """Return every connection with the catalog keys it bundles."""
    return [connection.to_dict() for connection in CONNECTIONS.values()]


def validate_public_settings(values: dict[str, Any]) -> dict[str, str]:
    """Reject values the UI should never send; returns {key: reason}.

    ``None`` (reset to default) is always allowed.
    """
    errors: dict[str, str] = {}
    for key, value in values.items():
        entry = CATALOG.get(key)
        if entry is None or value is None:
            continue
        if entry.input_type == "json" and not isinstance(value, dict):
            errors[key] = "must be a JSON object"
        if key == "webauthn_rp_name" and (not isinstance(value, str) or "://" in value):
            errors[key] = "must be a display name, not a URL"
        if key == "telegram_mode" and value not in ("polling", "webhook"):
            errors[key] = "must be 'polling' or 'webhook'"
        if key.startswith("scalable_") and entry.input_type == "number":
            # The worker turns these into intervals and timeouts; a value it
            # cannot honour is refused here instead of failing every run.
            number = None if isinstance(value, bool) else _finite_number(value)
            if number is None:
                errors[key] = "must be a number"
            elif (entry.min is not None and number < entry.min) or (entry.max is not None and number > entry.max):
                errors[key] = f"must be between {entry.min} and {entry.max}"
    return errors


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# -- Value normalizers (applied on read AND write by services.settings) --

def normalize_church_tax(value: Any) -> str:
    """Normalize a church-tax setting to the canonical decimal-fraction form.

    Canonical internal representation is "0.08"/"0.09" (cockpit + frontend tax
    form convention); legacy integer-percent values ("8"/"9") are accepted on
    write and normalized transparently on read. "none", empty, zero, and
    unparseable values degrade to "none" — the same outcome
    ``tax_cockpit._church_rate`` produced for them before, so no computed tax
    number changes.
    """
    if value is None:
        return "none"
    text = str(value).strip()
    if not text or text.lower() == "none":
        return "none"
    try:
        rate = Decimal(text)
    except InvalidOperation:
        return "none"
    if rate == 0:
        return "none"
    if rate > 1:
        # Integer-percent form ("8"/"9") → decimal fraction.
        rate = rate / Decimal("100")
    return format(rate.normalize(), "f")
