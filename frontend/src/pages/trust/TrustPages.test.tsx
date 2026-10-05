import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, Navigate, RouterProvider } from "react-router-dom";
import type { TrustTypeVerdict, TrustVerdict } from "../../lib/api";
import { AccuracyTab } from "../mandates/AccuracyTab";
import { MandatesTab } from "./MandatesTab";
import { TrustLayout } from "./TrustLayout";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

// Heavy pages reused by the Ideas / Advisor / Evidence tabs are stubbed: they are covered by their own tests.
vi.mock("../../components/discover/SkillTrendPanel", () => ({ SkillTrendPanel: () => <div>skill trend panel</div> }));
vi.mock("../advisor/EvolutionTab", () => ({ EvolutionTab: () => <div>evolution tab</div> }));
vi.mock("../EvidencePage", () => ({ EvidencePage: () => <div>evidence page</div> }));
vi.mock("../../components/charts/EChart", () => ({
  EChart: ({ ariaLabel }: { ariaLabel: string }) => <div role="img" aria-label={ariaLabel} />,
}));

import { AdvisorTab } from "./AdvisorTab";
import { EvidenceTab } from "./EvidenceTab";
import { IdeasTab } from "./IdeasTab";

function row(type: TrustTypeVerdict["type"], over: Partial<TrustTypeVerdict> = {}): TrustTypeVerdict {
  return {
    type,
    label: { ideas: "Ideas (Discover)", advisor: "Advisor", mandates: "Mandates", regime: "Regime" }[type],
    n: 0,
    n_needed: 617,
    n_issue_days: 0,
    hits: 0,
    hit_rate: null,
    hit_ci: null,
    mean_excess: null,
    mean_excess_ci: null,
    e_skill: 1,
    e_harm: 1,
    e_skill_crossed_strong: false,
    state: "too_early",
    benchmarked: type !== "mandates" && type !== "regime",
    benchmark_label: "x",
    brier: null,
    bss: null,
    bss_ci: null,
    n_stated_p: 0,
    spiegelhalter_z: null,
    reliability: [],
    range_coverage: null,
    calibration_caption: null,
    next_resolution_at: null,
    note: null,
    ...over,
  };
}

const verdict: TrustVerdict = {
  headline: "Too early to tell: no calls have resolved yet.",
  resolved_calls: 0,
  skill: null,
  state: "too_early",
  frozen_at_issue: true,
  method_note: "note",
  types: [row("ideas", { n: 3, hits: 2, hit_rate: 2 / 3, hit_ci: [0.15, 0.98] }), row("advisor"), row("mandates", { n: 40, hits: 22, hit_rate: 0.55, state: "no_evidence" }), row("regime")],
};

function respond(url: string) {
  if (url === "/api/trust/verdict") return Promise.resolve(verdict);
  if (url === "/api/llm-portfolio/") return Promise.resolve([{ id: "pA", name: "A", mandate: "A", managed_by: "llm", total_value: "1", holdings_count: 1, cash_balance: "1" }]);
  if (url === "/api/llm-portfolio/pA/accuracy")
    return Promise.resolve({ portfolio_id: "pA", mandate: "A", counts: { hit: 22, miss: 14, partial: 4, unresolvable: 2 }, resolved: 40, hit_rate: 0.55, pending_scoring: 3, estimate: true, not_financial_advice: true });
  return Promise.resolve({});
}

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/trust",
        element: <TrustLayout />,
        children: [
          { index: true, element: <Navigate to="verdict" replace /> },
          { path: "verdict", element: <div>verdict page</div> },
          { path: "ideas", element: <IdeasTab /> },
          { path: "advisor", element: <AdvisorTab /> },
          { path: "mandates", element: <MandatesTab />, children: [{ index: true, element: <AccuracyTab /> }] },
          { path: "evidence", element: <EvidenceTab /> },
          { path: "calls", element: <div>calls page</div> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

describe("Can I trust it? pages", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockImplementation(respond);
  });

  it("the layout has one route tab per section, and /trust lands on the verdict", async () => {
    renderAt("/trust");
    expect(await screen.findByText("verdict page")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Can I trust it?" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Trust sections" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((l) => [l.textContent, l.getAttribute("href")])).toEqual([
      ["Verdict", "/trust/verdict"],
      ["Ideas", "/trust/ideas"],
      ["Advisor", "/trust/advisor"],
      ["Mandates", "/trust/mandates"],
      ["Evidence", "/trust/evidence"],
      ["Calls", "/trust/calls"],
    ]);
    expect(within(nav).getByRole("link", { name: "Verdict" })).toHaveAttribute("aria-current", "page");
  });

  it("tabs are routes: clicking one changes the URL", async () => {
    const user = userEvent.setup();
    const router = renderAt("/trust/verdict");
    await screen.findByText("verdict page");
    await user.click(screen.getByRole("link", { name: "Calls" }));
    expect(router.state.location.pathname).toBe("/trust/calls");
    expect(await screen.findByText("calls page")).toBeInTheDocument();
  });

  it("Ideas: the verdict numbers sit on top of the existing Discover skill view", async () => {
    renderAt("/trust/ideas");
    expect(await screen.findByText("skill trend panel")).toBeInTheDocument();
    const ideas = await screen.findByTestId("type-ideas");
    expect(ideas).toHaveTextContent("Too early (3 of ~600)");
    expect(ideas).toHaveTextContent("67 % (2 of 3)");
  });

  it("Advisor: the verdict numbers sit on top of the existing evolution view", async () => {
    renderAt("/trust/advisor");
    expect(await screen.findByText("evolution tab")).toBeInTheDocument();
    expect(await screen.findByTestId("type-advisor")).toBeInTheDocument();
  });

  it("Mandates: reuses the Mandates accuracy tab under a mandate switch", async () => {
    const user = userEvent.setup();
    renderAt("/trust/mandates");

    expect(await screen.findByTestId("type-mandates")).toHaveTextContent("No evidence yet");
    // The reused AccuracyTab got its portfolio from the outlet context.
    expect(await screen.findByText("Resolved Decisions")).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith("/api/llm-portfolio/pA/accuracy");

    await user.click(screen.getByRole("button", { name: "Mandate B" }));
    expect(screen.getByRole("button", { name: "Mandate B" })).toHaveAttribute("aria-pressed", "true");
    // Mandate B has no portfolio in this fixture, so the reused tab says so.
    expect(await screen.findByText("No Mandate B portfolio yet")).toBeInTheDocument();
  });

  it("Evidence: reuses the Evidence page", async () => {
    renderAt("/trust/evidence");
    expect(await screen.findByText("evidence page")).toBeInTheDocument();
  });

  it("the numbers block degrades gracefully when the verdict endpoint fails", async () => {
    apiMock.mockImplementation((url: string) => (url === "/api/trust/verdict" ? Promise.reject(new Error("down")) : respond(url)));
    renderAt("/trust/ideas");
    expect(await screen.findByText(/track-record numbers are unavailable/)).toBeInTheDocument();
    expect(screen.getByText("skill trend panel")).toBeInTheDocument();
  });
});
