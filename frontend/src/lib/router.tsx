import { type ComponentType } from "react";
import { createBrowserRouter, Navigate, type RouteObject } from "react-router-dom";
import { AppShell } from "../components/layout/AppShell";
import { LoginPage } from "../pages/LoginPage";
import { SetupWizard } from "../pages/SetupWizard";
import { RouteErrorBoundary } from "../components/composed/ErrorBoundary";

// CSR-only app — no server-rendered HTML to hydrate.  React Router v7 warns
// when lazy routes lack a HydrateFallback; this helper silences the warning
// by providing a null fallback for every lazy route.
const _NullFallback = () => null;

// After a deploy, Vite's per-build content hashes mean an already-open tab
// (or a stale service-worker precache) can reference a chunk file that no
// longer exists on disk, so every lazy import in that tab fails with
// "Failed to fetch dynamically imported module". The fix is a fresh
// navigation: it re-fetches index.html, which points at the current build's
// hashes. Guard with sessionStorage so a genuinely broken chunk doesn't
// reload-loop forever.
async function withChunkErrorReload<T>(loader: () => Promise<T>): Promise<T> {
  try {
    return await loader();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const isChunkLoadError = /dynamically imported module|Importing a module script failed/i.test(message);
    const key = "chunk-error-reload-at";
    const last = Number(sessionStorage.getItem(key) ?? 0);
    if (isChunkLoadError && Date.now() - last > 10_000) {
      sessionStorage.setItem(key, String(Date.now()));
      window.location.reload();
      // Reload is in flight; never resolve so the router doesn't render an error state first.
      return new Promise<T>(() => {});
    }
    throw error;
  }
}

function lazyRoute(loader: () => Promise<{ default: ComponentType }>) {
  return {
    lazy: async () => ({
      Component: (await withChunkErrorReload(loader)).default,
      HydrateFallback: _NullFallback,
    }),
  };
}
function lazyNamed(loader: () => Promise<Record<string, ComponentType>>, name: string) {
  return {
    lazy: async () => ({
      Component: (await withChunkErrorReload(loader))[name],
      HydrateFallback: _NullFallback,
    }),
  };
}

export const appChildren: RouteObject[] = [
  { index: true, ...lazyNamed(() => import("../pages/HomePage"), "HomePage"), handle: { title: "Home" } },
  { path: "decide", ...lazyNamed(() => import("../pages/decide/DecidePage"), "DecidePage"), handle: { title: "Decide" } },
  {
    path: "dashboard",
    ...lazyNamed(() => import("../pages/Dashboard"), "Dashboard"),
    handle: { title: "Overview" },
  },
  { path: "portfolio", ...lazyNamed(() => import("../pages/portfolio/PortfolioLayout"), "PortfolioLayout"), handle: { title: "Portfolio" }, children: [
    { index: true, element: <Navigate to="holdings" replace /> },
    { path: "holdings", ...lazyNamed(() => import("../pages/portfolio/HoldingsTab"), "HoldingsTab"), handle: { title: "Portfolio — Holdings" } },
    { path: "accounts", ...lazyNamed(() => import("../pages/portfolio/AccountsTab"), "AccountsTab"), handle: { title: "Portfolio — Accounts" } },
    { path: "activity", ...lazyNamed(() => import("../pages/portfolio/ActivityTab"), "ActivityTab"), handle: { title: "Portfolio — Activity" } },
    { path: "performance", ...lazyNamed(() => import("../pages/portfolio/PerformanceTab"), "PerformanceTab"), handle: { title: "Portfolio — Performance" } },
    { path: "risk", ...lazyNamed(() => import("../pages/portfolio/RiskTab"), "RiskTab"), handle: { title: "Portfolio — Risk" } },
    // /portfolio/paper lives in the Strategy lab's route group below (same URL): the paper book is research, not your money.
    // Trades has no tab of its own; Activity links to it.
    { path: "trades", ...lazyNamed(() => import("../pages/portfolio/TradesTab"), "TradesTab"), handle: { title: "Portfolio — Trades" } },
    { path: "drift", ...lazyNamed(() => import("../pages/portfolio/DriftTab"), "DriftTab"), handle: { title: "Portfolio — Targets & drift" } },
    { path: "analysis", ...lazyNamed(() => import("../pages/research/PortfolioReportPage"), "PortfolioReportPage"), handle: { title: "Portfolio Analysis" } },
  ] },
  { path: "watchlist", element: <Navigate to="/research/watchlist" replace /> },
  { path: "transactions", element: <Navigate to="/portfolio/activity" replace /> },
  { path: "tax", ...lazyRoute(() => import("../pages/tax/TaxCockpitLayout")), handle: { title: "Tax Cockpit" }, children: [
    { index: true, element: <Navigate to="overview" replace /> },
    { path: "overview", ...lazyNamed(() => import("../pages/tax/OverviewTab"), "OverviewTab"), handle: { title: "Tax Cockpit — Overview" } },
    { path: "lots", ...lazyNamed(() => import("../pages/tax/LotsTab"), "LotsTab"), handle: { title: "Tax Cockpit — Lots & events" } },
    { path: "box3", ...lazyNamed(() => import("../pages/tax/Box3Tab"), "Box3Tab"), handle: { title: "Tax Cockpit — Box 3" } },
    { path: "settings", ...lazyNamed(() => import("../pages/tax/SettingsTab"), "SettingsTab"), handle: { title: "Tax Cockpit — Settings" } },
  ] },
  { path: "research", ...lazyNamed(() => import("../pages/research/ResearchLayout"), "ResearchLayout"), handle: { title: "Watchlist" }, children: [
    // DiscoverHubPage (mounted at /discover) is a strict superset of the old
    // research/DiscoverTab — consolidated per unified-portfolio-engine Phase 8.
    { index: true, element: <Navigate to="/discover" replace /> },
    { path: "discover", element: <Navigate to="/discover" replace /> },
    { path: "watchlist", ...lazyNamed(() => import("../pages/research/WatchlistTab"), "WatchlistTab"), handle: { title: "Watchlist" } },
    { path: "library", ...lazyNamed(() => import("../pages/research/LibraryTab"), "LibraryTab"), handle: { title: "Watchlist — Library" } },
    { path: "generate", ...lazyNamed(() => import("../pages/research/GenerateTab"), "GenerateTab"), handle: { title: "Watchlist — Generate" } },
    { path: "paste", ...lazyNamed(() => import("../pages/research/PasteTab"), "PasteTab"), handle: { title: "Watchlist — Paste" } },
    { path: "stocks/:ticker", ...lazyNamed(() => import("../pages/research/StockDetailPage"), "StockDetailPage"), handle: { title: "Stock Detail" } },
  ] },
  { path: "research/portfolio-analysis", element: <Navigate to="/portfolio/analysis" replace /> },
  { path: "discover", ...lazyNamed(() => import("../pages/DiscoverHubPage"), "DiscoverHubPage"), handle: { title: "Discover" } },
  { path: "recommendations", element: <Navigate to="/discover?tab=shortlist" replace /> },
  { path: "pipeline", element: <Navigate to="/discover?tab=track-record" replace /> },
  { path: "alphacrafter", element: <Navigate to="/discover" replace /> },
  { path: "verification", element: <Navigate to="/trust" replace /> },
  { path: "trust", ...lazyNamed(() => import("../pages/trust/TrustLayout"), "TrustLayout"), handle: { title: "Can I trust it?" }, children: [
    { index: true, element: <Navigate to="verdict" replace /> },
    { path: "verdict", ...lazyNamed(() => import("../pages/trust/VerdictTab"), "VerdictTab"), handle: { title: "Can I trust it? — Verdict" } },
    { path: "ideas", ...lazyNamed(() => import("../pages/trust/IdeasTab"), "IdeasTab"), handle: { title: "Can I trust it? — Ideas" } },
    { path: "advisor", ...lazyNamed(() => import("../pages/trust/AdvisorTab"), "AdvisorTab"), handle: { title: "Can I trust it? — Advisor" } },
    { path: "mandates", ...lazyNamed(() => import("../pages/trust/MandatesTab"), "MandatesTab"), handle: { title: "Can I trust it? — Mandates" }, children: [
      { index: true, ...lazyNamed(() => import("../pages/mandates/AccuracyTab"), "AccuracyTab") },
    ] },
    { path: "evidence", ...lazyNamed(() => import("../pages/trust/EvidenceTab"), "EvidenceTab"), handle: { title: "Can I trust it? — Evidence" } },
    { path: "calls", ...lazyNamed(() => import("../pages/trust/CallsTab"), "CallsTab"), handle: { title: "Can I trust it? — Calls" } },
  ] },
  { path: "profile", element: <Navigate to="/settings/profile" replace /> },
  { path: "news", ...lazyNamed(() => import("../pages/news/NewsPage"), "NewsPage"), handle: { title: "News" } },
  // The chat page has no PageHeader, so the shell supplies its h1 (see useRouteMeta).
  { path: "chat", ...lazyNamed(() => import("../pages/chat/ChatPage"), "ChatPage"), handle: { title: "AI Chat", noPageHeader: true } },
  { path: "llm-portfolio", element: <Navigate to="/advisor" replace /> },
  // Report Phase 5: the Strategy lab. A pathless layout, so every tab keeps its URL.
  { path: "lab", element: <Navigate to="/evidence" replace /> },
  { ...lazyNamed(() => import("../pages/lab/StrategyLabLayout"), "StrategyLabLayout"), children: [
    { path: "evidence", ...lazyNamed(() => import("../pages/EvidencePage"), "EvidencePage"), handle: { title: "Strategy lab — Evidence" } },
    { path: "advisor", ...lazyNamed(() => import("../pages/advisor/PaperPortfoliosHub"), "PaperPortfoliosHub"), handle: { title: "Strategy lab — LLM advisor" }, children: [
      { index: true, element: <Navigate to="loop" replace /> },
      { path: "portfolios", element: <Navigate to="/advisor/loop" replace /> },
      { path: "advisor", element: <Navigate to="/advisor/loop" replace /> },
      { path: "loop", ...lazyNamed(() => import("../pages/advisor/AdvisorLoopTab"), "AdvisorLoopTab"), handle: { title: "Advisor — Loop" } },
      { path: "evolution", ...lazyNamed(() => import("../pages/advisor/EvolutionTab"), "EvolutionTab"), handle: { title: "Advisor — Evolution" } },
      { path: "diagnostics", ...lazyNamed(() => import("../pages/advisor/DiagnosticsTab"), "DiagnosticsTab"), handle: { title: "Advisor — Diagnostics" } },
      { path: "competition", element: <Navigate to="/advisor/evolution" replace /> },
      { path: "reviews", element: <Navigate to="/advisor/evolution" replace /> },
      { path: "divergence", ...lazyNamed(() => import("../pages/advisor/DivergenceTab"), "DivergenceTab"), handle: { title: "Advisor — What would change" } },
      { path: "learnings", element: <Navigate to="/advisor/evolution" replace /> },
    ] },
    { path: "mandates", ...lazyNamed(() => import("../pages/mandates/MandatesHub"), "MandatesHub"), handle: { title: "Strategy lab — Mandates" }, children: [
      { index: true, element: <Navigate to="overview" replace /> },
      { path: "overview", ...lazyNamed(() => import("../pages/mandates/OverviewTab"), "OverviewTab"), handle: { title: "Mandates — Overview" } },
      { path: "journal", ...lazyNamed(() => import("../pages/mandates/JournalTab"), "JournalTab"), handle: { title: "Mandates — Journal" } },
      { path: "accuracy", ...lazyNamed(() => import("../pages/mandates/AccuracyTab"), "AccuracyTab"), handle: { title: "Mandates — Accuracy" } },
      { path: "divergence", ...lazyNamed(() => import("../pages/mandates/DivergenceTab"), "DivergenceTab"), handle: { title: "Mandates — Divergence" } },
    ] },
    { path: "graduation", ...lazyNamed(() => import("../pages/GraduationPage"), "GraduationPage"), handle: { title: "Strategy lab — Graduation" } },
    // The paper book used to be a Portfolio tab; the URL is unchanged.
    { path: "portfolio/paper", ...lazyNamed(() => import("../pages/portfolio/PaperPortfolioTab"), "PaperPortfolioTab"), handle: { title: "Strategy lab — Paper portfolio", noPageHeader: true } },
  ] },
  { path: "competition", element: <Navigate to="/advisor/evolution" replace /> },
  { path: "plan", ...lazyNamed(() => import("../pages/ThisMonthPage"), "ThisMonthPage"), handle: { title: "This month" } },
  { path: "money", ...lazyNamed(() => import("../pages/money/MoneyLayout"), "MoneyLayout"), handle: { title: "Budget" }, children: [
    { index: true, element: <Navigate to="overview" replace /> },
    { path: "overview", ...lazyNamed(() => import("../pages/money/OverviewTab"), "OverviewTab"), handle: { title: "Budget \u2014 Overview" } },
    { path: "envelopes", ...lazyNamed(() => import("../pages/money/EnvelopeBudgetsTab"), "EnvelopeBudgetsTab"), handle: { title: "Budget \u2014 Envelopes" } },
    { path: "income", ...lazyNamed(() => import("../pages/money/IncomeTab"), "IncomeTab"), handle: { title: "Budget \u2014 Income" } },
    { path: "expenses", ...lazyNamed(() => import("../pages/money/ExpensesTab"), "ExpensesTab"), handle: { title: "Budget \u2014 Expenses" } },
    { path: "subscriptions", ...lazyNamed(() => import("../pages/money/SubscriptionsTab"), "SubscriptionsTab"), handle: { title: "Budget \u2014 Subscriptions" } },
    { path: "invoices", ...lazyNamed(() => import("../pages/money/InvoicesTab"), "InvoicesTab"), handle: { title: "Budget \u2014 Invoices" } },
  ] },
  // Quant Lab has no PageHeader, so the shell supplies its h1 (see useRouteMeta).
  { path: "quantlab", ...lazyNamed(() => import("../pages/quantlab/QuantLabLayout"), "QuantLabLayout"), handle: { noPageHeader: true }, children: [
    { index: true, element: <Navigate to="overview" replace /> },
    { path: "overview", ...lazyNamed(() => import("../pages/quantlab/views/OverviewView"), "OverviewView"), handle: { title: "Quant Lab \u2014 Overview" } },
    { path: "risk", ...lazyNamed(() => import("../pages/quantlab/views/RiskView"), "RiskView"), handle: { title: "Quant Lab \u2014 Risk" } },
    { path: "optimisation", ...lazyNamed(() => import("../pages/quantlab/views/OptimisationView"), "OptimisationView"), handle: { title: "Quant Lab \u2014 Optimisation" } },
    { path: "scenarios", ...lazyNamed(() => import("../pages/quantlab/views/ScenariosView"), "ScenariosView"), handle: { title: "Quant Lab \u2014 Monte Carlo" } },
    { path: "correlation", ...lazyNamed(() => import("../pages/quantlab/views/CorrelationFactorsView"), "CorrelationFactorsView"), handle: { title: "Quant Lab \u2014 Correlation" } },
    { path: "backtest", ...lazyNamed(() => import("../pages/quantlab/views/BacktestView"), "BacktestView"), handle: { title: "Quant Lab \u2014 Backtest" } },
    { path: "runs", ...lazyNamed(() => import("../pages/quantlab/views/RunsView"), "RunsView"), handle: { title: "Quant Lab \u2014 Runs" } },
    { path: "experiments", ...lazyNamed(() => import("../pages/quantlab/views/ExperimentsView"), "ExperimentsView"), handle: { title: "Quant Lab \u2014 Experiments" } },
    { path: "attribution", ...lazyNamed(() => import("../pages/quantlab/views/AttributionView"), "AttributionView"), handle: { title: "Quant Lab \u2014 Attribution" } },
    { path: "holdings", ...lazyNamed(() => import("../pages/quantlab/views/RealHoldingsView"), "RealHoldingsView"), handle: { title: "Quant Lab \u2014 Holdings" } },
    { path: "regime", ...lazyNamed(() => import("../pages/quantlab/views/RegimeSignalsView"), "RegimeSignalsView"), handle: { title: "Quant Lab \u2014 Regime Signals" } },
    { path: "goals", ...lazyNamed(() => import("../pages/quantlab/views/GoalsView"), "GoalsView"), handle: { title: "Quant Lab \u2014 Goals" } },
    { path: "research", ...lazyNamed(() => import("../pages/quantlab/views/ResearchView"), "ResearchView"), handle: { title: "Quant Lab \u2014 Research Library" } },
    { path: "llm-research", ...lazyNamed(() => import("../pages/quantlab/views/LlmResearchView"), "LlmResearchView"), handle: { title: "Quant Lab \u2014 LLM Research" } },
    { path: "intelligence", ...lazyNamed(() => import("../pages/quantlab/views/MarketIntelligenceView"), "MarketIntelligenceView"), handle: { title: "Quant Lab \u2014 Market Intelligence" } },
    { path: "factors", ...lazyNamed(() => import("../pages/quantlab/views/FactorDashboardView"), "FactorDashboardView"), handle: { title: "Quant Lab \u2014 Factor Dashboard" } },
    { path: "verification", element: <Navigate to="/trust" replace /> },
  ]},
  { path: "imports", ...lazyNamed(() => import("../pages/imports/ImportsPage"), "ImportsPage"), handle: { title: "Imports & Reconciliation" } },
  { path: "settings", ...lazyNamed(() => import("../pages/settings/SettingsLayout"), "SettingsLayout"), handle: { title: "Control Center" }, children: [
    { index: true, ...lazyNamed(() => import("../pages/settings/OverviewPage"), "OverviewPage") },
    { path: "status", ...lazyNamed(() => import("../pages/settings/StatusPage"), "StatusPage"), handle: { title: "Control Center \u2014 Status" } },
    { path: ":page", ...lazyNamed(() => import("../pages/settings/SettingsPage"), "SettingsPage") },
  ]},
  { path: "budget", element: <Navigate to="/money/overview" replace /> },
  { path: "expenses", element: <Navigate to="/money/expenses" replace /> },
  { path: "subscriptions", element: <Navigate to="/money/subscriptions" replace /> },
  { path: "invoices", element: <Navigate to="/money/invoices" replace /> },
  { path: "reports", element: <Navigate to="/research/library" replace /> },
  { path: "quant", element: <Navigate to="/quantlab/overview" replace /> },
  { path: "quant-portfolio", element: <Navigate to="/quantlab/overview" replace /> },
  { path: "review", element: <Navigate to="/" replace /> },
  { path: "inbox", element: <Navigate to="/" replace /> },
];

export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage />, errorElement: <RouteErrorBoundary /> },
  { path: "/setup", element: <SetupWizard />, errorElement: <RouteErrorBoundary /> },
  // The pathless child catches page errors inside the shell, so a crashing page
  // keeps the nav (on a phone, the only way out); the outer one covers AppShell.
  { path: "/", element: <AppShell />, errorElement: <RouteErrorBoundary />, HydrateFallback: () => null, children: [
    { errorElement: <RouteErrorBoundary />, children: appChildren },
  ] },
  { path: "*", element: <Navigate to="/" replace /> },
];

export const router = createBrowserRouter(routes, {
  future: { v7_startTransition: true },
});
