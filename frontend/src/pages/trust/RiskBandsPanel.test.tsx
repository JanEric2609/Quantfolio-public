import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TrustRiskBands } from "../../lib/api";
import { RiskBandsPanel } from "./RiskBandsPanel";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

const horizon = (over: Partial<TrustRiskBands["targets"][number]["horizons"][number]> = {}) => ({
  horizon_days: 1,
  forecast_day: "2026-10-14",
  sigma: 0.01,
  var95: 0.016,
  es975: 0.024,
  unpriced_share: 0,
  n_scored: 40,
  breaches: 2,
  coverage_low: 0.9,
  coverage_high: 0.99,
  e_var: 0.8,
  e_es: 1.1,
  ...over,
});

function renderPanel(data: TrustRiskBands) {
  apiMock.mockReset();
  apiMock.mockResolvedValue(data);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RiskBandsPanel />
    </QueryClientProvider>,
  );
}

describe("RiskBandsPanel", () => {
  beforeEach(() => apiMock.mockReset());

  it("says when there is no forecast yet", async () => {
    renderPanel({ targets: [], note: "" });
    expect(await screen.findByText(/No risk forecast yet/)).toBeInTheDocument();
  });

  it("shows the bands, the score so far and a verdict per horizon", async () => {
    renderPanel({
      note: "Forecasts are stored before the fact.",
      targets: [
        {
          target: "paper:p1",
          label: "Mandate A",
          horizons: [horizon(), horizon({ horizon_days: 21, n_scored: 3, breaches: 1, e_var: 25, unpriced_share: 0.2 })],
        },
      ],
    });
    expect(await screen.findByText("Mandate A")).toBeInTheDocument();
    expect(screen.getByText("No sign of trouble")).toBeInTheDocument();
    expect(screen.getByText("Bands too narrow")).toBeInTheDocument();
    expect(screen.getByText(/20 % of the value has no stored prices/)).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith("/api/trust/risk-bands");
  });
});
