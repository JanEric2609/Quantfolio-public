import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { CrashGuardCard } from "./CrashGuardBanner";
import type { CrashGuardState } from "../../lib/api";

const base: CrashGuardState = {
  active: true,
  market: {
    ticker: "EUNL.DE", as_of: "2026-10-14", close: 80, peak: 100, peak_date: "2026-02-01",
    drawdown: 0.22, level: "bear", level_label: "Bear market", history_start: "2009-09-25",
  },
  episodes: [
    { peak_date: "2020-02-19", trough_date: "2020-03-23", depth: 0.33, months_to_trough: 1.1, recovery_date: "2020-11-16", months_to_recover: 7.8 },
    { peak_date: "2026-02-01", trough_date: "2026-10-14", depth: 0.22, months_to_trough: 8.4, recovery_date: null, months_to_recover: null },
  ],
  message: "MSCI World is 22% below its high.",
  plan_note: "Sell nothing.",
  behaviour: [{ id: "core-sold", kind: "core_sold", title: "Core ETF units sold during the fall (10% of the position)", detail: "Selling after a fall…" }],
};

function renderCard(data: CrashGuardState) {
  return render(<MemoryRouter><CrashGuardCard data={data} /></MemoryRouter>);
}

describe("CrashGuardCard", () => {
  it("shows the level, the owner's rule and what broke the plan", () => {
    renderCard(base);
    expect(screen.getByRole("heading", { name: "Bear market: markets are 22 % below their high" })).toBeInTheDocument();
    expect(screen.getByText("Sell nothing.")).toBeInTheDocument();
    expect(screen.getByText(/Core ETF units sold/)).toBeInTheDocument();
  });

  it("lists only recovered falls in the history table", () => {
    renderCard(base);
    expect(screen.getByText(/Earlier falls of 10 % or more since 2009 \(1\)/)).toBeInTheDocument();
  });

  it("points to the profile when no rule is written yet", () => {
    renderCard({ ...base, plan_note: null });
    expect(screen.getByRole("link", { name: "Profile" })).toHaveAttribute("href", "/settings/profile");
  });

  it("says the prices are stale instead of judging the market on them", () => {
    renderCard({ ...base, active: false, stale: true, stale_since: "2026-10-08", behaviour: [] });
    expect(screen.getByRole("heading", { name: "No fresh market prices since 2026-10-08" })).toBeInTheDocument();
    expect(screen.getByTestId("crash-guard-stale")).toBeInTheDocument();
  });
});
