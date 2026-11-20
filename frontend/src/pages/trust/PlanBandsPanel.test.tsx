import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TrustPlanBandModel, TrustPlanBands } from "../../lib/api";
import { PlanBandsPanel } from "./PlanBandsPanel";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

const model = (over: Partial<TrustPlanBandModel> = {}): TrustPlanBandModel => ({
  model: "planner",
  label: "The projection's model",
  latest: {
    forecast_day: "2026-10-15",
    end_day: "2027-10-15",
    p10: -0.15,
    p50: 0.04,
    p90: 0.25,
    start_value: 20000,
    monthly_contribution: 100,
    value_p10: 18000,
    value_p50: 22000,
    value_p90: 26000,
  },
  n_bands: 1,
  n_scored: 0,
  inside: 0,
  below: 0,
  above: 0,
  n_independent: 0,
  coverage_low: null,
  coverage_high: null,
  first_check: "2027-10-15",
  ...over,
});

function renderPanel(data: TrustPlanBands) {
  apiMock.mockReset();
  apiMock.mockResolvedValue(data);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PlanBandsPanel />
    </QueryClientProvider>,
  );
}

describe("PlanBandsPanel", () => {
  beforeEach(() => apiMock.mockReset());

  it("says when there is no band yet", async () => {
    renderPanel({ models: [], note: "", estimate: true });
    expect(await screen.findByText(/No plan band yet/)).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith("/api/trust/plan-bands");
  });

  it("shows the band, when it is first checked and a verdict once years have been scored", async () => {
    renderPanel({
      note: "Stored before the year starts.",
      estimate: true,
      models: [
        model(),
        model({
          model: "long_history",
          label: "History since 1870",
          n_scored: 60,
          inside: 40,
          below: 15,
          above: 5,
          n_independent: 6,
          coverage_low: 0.3,
          coverage_high: 0.7,
          first_check: null,
        }),
      ],
    });
    expect(await screen.findByText("The projection's model")).toBeInTheDocument();
    expect(screen.getByText(/the first band is checked on/)).toBeInTheDocument();
    expect(screen.getByText("Too early")).toBeInTheDocument();
    expect(screen.getByText("Band too narrow")).toBeInTheDocument();
    expect(screen.getByText(/40 inside, 15 below, 5 above/)).toBeInTheDocument();
    expect(screen.getAllByText(/-15 % and 25 %/)).toHaveLength(2);
  });
});
