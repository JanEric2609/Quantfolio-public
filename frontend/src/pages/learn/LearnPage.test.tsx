import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { LearnPage } from "./LearnPage";
import { compositeFor } from "./stations";

vi.mock("../../lib/api", () => ({
  api: () => Promise.reject(new Error("offline")),
  getMonthlyPlan: () => Promise.resolve({
    book_eur: 36000, has_holdings: true, contribution_eur: 1000,
    sleeves: [
      { key: "core", label: "Core", current_pct: 80, target_pct: 100, unlocked: true },
      { key: "tilt", label: "Factor tilt", current_pct: 0, target_pct: 0, unlocked: false },
    ],
  }),
  getCrashGuard: () => Promise.reject(new Error("offline")),
  getLadderState: () => Promise.resolve({ candidates: [] }),
}));
vi.mock("../quantlab/hooks/useProjection", () => ({ useProjection: () => ({ data: undefined, isError: true }) }));
vi.mock("../trust/useTrust", () => ({ useTrustVerdict: () => ({ data: undefined, isLoading: false }) }));

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><LearnPage /></MemoryRouter></QueryClientProvider>);
}

describe("LearnPage", () => {
  beforeEach(() => window.localStorage.clear());

  it("typesets every lesson's formula and links one checked reading per lesson", async () => {
    renderPage();
    const ids = ["returns", "vol", "beta", "tstat", "nw", "multi", "wf", "eproc", "shrink", "mc", "tax"];
    for (const id of ids) {
      const lesson = await screen.findByTestId(`lesson-${id}`);
      expect(lesson.querySelector(".katex")).not.toBeNull();
      const link = screen.getByTestId(`lesson-reading-${id}`).querySelector("a");
      expect(link?.getAttribute("href")).toMatch(/^https:\/\//);
      expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    }
  });

  it("shows eight stations and skips the ones whose data is empty, with the reason", async () => {
    renderPage();
    expect(await screen.findByTestId("station-where")).toBeInTheDocument();
    for (const id of ["where", "plan", "longrun", "why", "hit", "ladder", "crash", "tax"]) {
      expect(screen.getByTestId(`station-${id}`)).toBeInTheDocument();
    }
    expect(await screen.findByTestId("station-ladder-empty")).toHaveTextContent("No candidate on the ladder yet");
    expect(document.querySelectorAll("h1").length).toBe(1);
  });

  it("hides the live figure until the reader has answered, then shows it and remembers the answer", async () => {
    renderPage();
    const slider = await screen.findByLabelText("Your guess for the core share");
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.change(slider, { target: { value: "50" } });
    expect(await screen.findByText(/It is 80 % of/)).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem("quantfolio:learn") ?? "{}").answers.where).toBe(50);
  });

  it("keeps the tax figure labelled as an estimate, not advice", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("Sale proceeds"), { target: { value: "12000" } });
    expect(screen.getByText(/Estimate, not tax advice/)).toBeInTheDocument();
  });

  it("recomputes a composite from the reader's weights", () => {
    const inputs = [{ signal: "momentum", score: 0.9 }, { signal: "risk", score: 0.1 }];
    expect(compositeFor(inputs, {})).toBeCloseTo(0.5);
    expect(compositeFor(inputs, { risk: 0 })).toBeCloseTo(0.9);
  });
});
