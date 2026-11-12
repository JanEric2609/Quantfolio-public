import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TrustForwardLab } from "../../lib/api";
import { ForwardLabPanel } from "./ForwardLabPanel";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

const data: TrustForwardLab = {
  start: "2026-11-10",
  alpha: 0.01,
  threshold_single: 100,
  note: "Before trading costs; daily rebalanced; recorded forward only.",
  signals: [
    {
      signal: "momentum_12_1",
      label: "12-1 month momentum",
      source: "Jegadeesh & Titman 1993",
      days: 120,
      mean_daily: 0.0004,
      ci90_daily: [-0.0001, 0.0009],
      e_skill: 3.2,
      e_harm: 0.4,
      names: 1210,
      state: "collecting",
      robustness: { false_alarm: 0.012, without_best_days: 1.1, first_half: 2.0, second_half: 1.6 },
    },
    {
      signal: "news_tone",
      label: "Overnight AI reading of the headlines (7 nights)",
      source: "Lopez-Lira & Tang 2023",
      days: 0,
      e_skill: 1,
      e_harm: 1,
      names: 0,
      state: "not_started",
    },
  ],
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ForwardLabPanel />
    </QueryClientProvider>,
  );
}

describe("ForwardLabPanel", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockResolvedValue(data);
  });

  it("shows each signal's state, evidence and stress checks", async () => {
    renderPanel();
    const row = await screen.findByTestId("forward-lab-momentum_12_1");
    expect(row).toHaveTextContent("Collecting");
    expect(row).toHaveTextContent("120 days on 1210 stocks");
    expect(row).toHaveTextContent("works 3,20");
    expect(row).toHaveTextContent("1,2 %");
    expect(screen.getByTestId("forward-lab-news_tone")).toHaveTextContent("No day scored yet.");
    expect(screen.getByText(/passes 200 for the strongest of the 2/)).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith("/api/trust/forward-lab");
  });

  it("says when the lab is unavailable", async () => {
    apiMock.mockRejectedValue(new Error("down"));
    renderPanel();
    expect(await screen.findByText("The forward-test lab is unavailable right now.")).toBeInTheDocument();
  });
});
