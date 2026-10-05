import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SkillTrendPanel } from "./SkillTrendPanel";
import type { DiscoverySkillSnapshot, DiscoverySkillSummary } from "../../lib/api";

const summary: DiscoverySkillSummary = {
  benchmark: "EUNL.DE",
  resolved: 0,
  resolved_vs_benchmark: 0,
  pending: 121,
  next_resolve_at: "2026-10-15T05:00:00+00:00",
  hit_rate: null,
  mean_excess_return: null,
  mean_rank_ic: null,
  icir: null,
  ic_t_stat: null,
  ic_dates: 0,
  min_ic_names: 5,
};

let current: DiscoverySkillSummary = summary;
let trend: DiscoverySkillSnapshot[] = [];
const snapshot: DiscoverySkillSnapshot = {
  id: "s1", snapshot_at: "2026-10-20T05:00:00+00:00", metric_type: "discover", total_predictions: 130,
  total_resolved: 12, hit_rate: 0.75, brier_score_avg: null, rank_ic: 0.2, icir: null, ece: null,
  mincer_a0: null, mincer_a1: null,
};
vi.mock("../../lib/api", () => ({
  getSkillTrend: async () => trend,
  getSkillSummary: async () => current,
}));

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SkillTrendPanel />
    </QueryClientProvider>,
  );
}

describe("SkillTrendPanel", () => {
  it("says how many predictions are pending and what they are judged against", async () => {
    current = summary;
    trend = [];
    renderPanel();

    expect(await screen.findByText(/121 predictions are waiting for their horizon/)).toBeInTheDocument();
    expect(screen.getByText(/judged against EUNL\.DE/)).toBeInTheDocument();
  });

  it("withholds a hit rate and IC from a small sample and shows them once it is large enough", async () => {
    current = {
      ...summary, resolved: 12, resolved_vs_benchmark: 12, hit_rate: 0.75, mean_excess_return: 0.04,
      mean_rank_ic: 0.2, ic_t_stat: 2.5, ic_dates: 3,
    };
    trend = [snapshot];
    renderPanel();

    expect(await screen.findByText(/12 of 30 resolved calls needed/)).toBeInTheDocument();
    expect(screen.getByText(/3 of 12 scoring dates needed/)).toBeInTheDocument();
    expect(screen.getAllByText("Too early").length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText("75 %")).not.toBeInTheDocument();
  });

  it("keeps overlapping weekly dates from passing as a year of evidence", async () => {
    current = {
      ...summary, resolved: 60, resolved_vs_benchmark: 60, hit_rate: 0.6, mean_excess_return: 0.01,
      mean_rank_ic: 0.05, ic_t_stat: 2.4, ic_dates: 14, independent_windows: 3, min_independent_windows: 12,
      hit_rate_ci_half_width: null,
    };
    trend = [snapshot];
    renderPanel();

    expect(await screen.findByText(/60 resolved, but 3 of 12 independent months/)).toBeInTheDocument();
    expect(screen.getByText(/14 dates, but 3 of 12 independent months/)).toBeInTheDocument();
    expect(screen.queryByText(/t 2\.4/)).not.toBeInTheDocument();
  });

  it("shows the numbers with their uncertainty from 30 resolved calls", async () => {
    current = {
      ...summary, resolved: 40, resolved_vs_benchmark: 40, hit_rate: 0.6, mean_excess_return: 0.01,
      mean_rank_ic: 0.05, ic_t_stat: 1.1, ic_dates: 14,
    };
    trend = [snapshot];
    renderPanel();

    expect(await screen.findByText(/40 resolved · 50 % is chance · ±15 points/)).toBeInTheDocument();
    expect(screen.getByText(/0,050 \(t 1,1\)/)).toBeInTheDocument();
    expect(screen.queryByText("Too early")).not.toBeInTheDocument();
  });
});
