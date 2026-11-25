import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import * as api from "../lib/api";
import { verdictClass } from "../lib/signalVerdict";
import { SignalsPage } from "./SignalsPage";

const cell = (over: Partial<api.SignalCell>): api.SignalCell => ({
  cell: "ta_trend:cfd", registration: "events", n: 0, n_min: 40, mean_net: null, hit_rate: null, p_holm: 1,
  passes: false, verdict: "too early", placebo_percentile: null, placebo_p95: null, replay: null, real: null, ...over,
});
const dto = (over: Partial<api.SignalsDto> = {}): api.SignalsDto => ({
  estimate: true, not_tax_advice: true,
  registrations: { first: { hash: "a", registered_at: null }, events: { hash: "b", registered_at: null } },
  cells: [cell({}), cell({ cell: "ta_band:cfd", n: 45, mean_net: -0.4, verdict: "failing" }),
    cell({ cell: "index_momentum:cfd", registration: "first", replay: { n: 2000, mean_eur: -0.18 } })],
  open_positions: [], journal: [],
  bankroll: { month_eur: "-12", day_eur: "0", ok: true, reason: "", resumes: "", pot: "280", monthly_stop: "60", daily_stop: "15", max_open: 3, open_real: 0 },
  owner_call: null, limits: {}, ...over,
});

afterEach(() => vi.restoreAllMocks());

describe("SignalsPage", () => {
  it("maps verdicts to colours", () => {
    expect(verdictClass("failing")).toContain("red");
    expect(verdictClass("passing")).toContain("green");
    expect(verdictClass("too early")).toContain("muted");
    expect(verdictClass("not significant")).toContain("amber");
  });

  it("renders the cells with their verdicts and one h2", async () => {
    vi.spyOn(api, "getSignals").mockResolvedValue(dto());
    render(<MemoryRouter><SignalsPage /></MemoryRouter>);
    expect(await screen.findByText("ta_band:cfd")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    expect(screen.getAllByTestId("verdict").map((e) => e.textContent)).toEqual(["too early", "failing", "too early"]);
    expect(screen.getByText("n 2000, -0.18 EUR a trade")).toBeInTheDocument();
    expect(screen.getByText("No event position is open.")).toBeInTheDocument();
    expect(screen.getByText("No logged trades yet.")).toBeInTheDocument();
  });

  it("shows the backend's plain cell name and the pair shadow", async () => {
    const d = dto({
      cells: [cell({ name: "EMA cross" })],
      pair_shadow: { days_both_fired: 3, days_same_side: 2, mean_full_eur: -1.5, mean_half_eur: -0.75, worst_full_eur: -4, worst_half_eur: -2, note: "shadow only" },
    });
    vi.spyOn(api, "getSignals").mockResolvedValue(d);
    render(<MemoryRouter><SignalsPage /></MemoryRouter>);
    expect(await screen.findByText("EMA cross")).toBeInTheDocument();
    expect(screen.getByTestId("pair-shadow")).toHaveTextContent("2 days. Mean -1.50 EUR at full size");
  });

  it("shows the bankroll stop as an alert", async () => {
    const d = dto();
    d.bankroll = { ...d.bankroll, ok: false, reason: "the month's real loss reached 60 EUR", resumes: "2026-12-01" };
    vi.spyOn(api, "getSignals").mockResolvedValue(d);
    render(<MemoryRouter><SignalsPage /></MemoryRouter>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Paper only: the month's real loss reached 60 EUR");
  });

  it("says so when the API fails", async () => {
    vi.spyOn(api, "getSignals").mockRejectedValue(new Error("boom"));
    render(<MemoryRouter><SignalsPage /></MemoryRouter>);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
