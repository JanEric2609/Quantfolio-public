import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import * as api from "../lib/api";
import { IntradayPage } from "./IntradayPage";

const facts: api.T212Facts = {
  spread_bp: { DAX: "0.5", ES50: "3.8", SPX: "0.75", NDX: "0.6" }, margin_rate: "0.05", overnight_rate: "0.000083",
  fx_fee_on_result: "0.005", min_order_eur: "20", quantity_decimals: "4", observed: "2026-11-23",
};
const dax: api.IntradayIndex = {
  key: "DAX", label: "DAX", t212_name: "Germany 40 (GER40)", exchange: "XETR", currency: "EUR",
  session: { open: "2026-11-24T08:00:00+00:00", close: "2026-11-24T16:30:00+00:00", status: "open" },
  enter_at: "2026-11-24T16:00:00+00:00", exit_at: "2026-11-24T16:27:00+00:00", skip_reason: null,
  macro_events_today: [], r1: "0.0042", direction: "long", cfd_today: null, index_level_est: "25000",
  move_pct: "0.42", sigma: "0.01", sigma_points: "250", typical_30m_points: "61", spread_points: "1.25", eur_per_unit: "1",
};
const row: api.CfdJournalRow = {
  position_id: "abcdef12-0000", short_id: "abcdef12", created_at: "2026-11-24T16:00:00+00:00", market_key: "DAX",
  direction: "long", currency: "EUR", status: "sent", r1: "0.0042", index_level_est: "25000",
  quantity_suggested: "0.0200", quantity: null, exposure_eur: "500", stop_points: "250", stop_level: null,
  entry_level: null, exit_level: null, entered_at: null, closed_at: null, real_pnl_eur: null, paper_net_eur: null,
  paper_status: "open", nights_held: null, note: null,
};
const dto: api.IntradayDto = {
  board: { as_of: new Date().toISOString(), day: "2026-11-24", indices: [dax] }, board_age_s: 60, t212: facts, journal: [row],
};

afterEach(() => vi.restoreAllMocks());

describe("IntradayPage", () => {
  it("renders the board, the calculator and the journal from the API, with one h1", async () => {
    vi.spyOn(api, "getIntraday").mockResolvedValue(dto);
    render(<MemoryRouter><IntradayPage /></MemoryRouter>);
    expect(await screen.findByText("Germany 40 (GER40) · session open 09:00-17:30")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByText(/Typical last-30-minute move/)).toBeInTheDocument();
    // The calculator fills stop and level from the board: 5 EUR / 250 pts = 0.0200.
    expect(screen.getByTestId("calc-result")).toHaveTextContent("Quantity 0.0200");
    expect(screen.getByText("Your journal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Entered" })).toBeInTheDocument();
  });

  it("warns when the board is older than ten minutes and handles an empty board", async () => {
    vi.spyOn(api, "getIntraday").mockResolvedValue({ ...dto, board_age_s: 1200 });
    const { unmount } = render(<MemoryRouter><IntradayPage /></MemoryRouter>);
    expect(await screen.findByText(/older than 10 minutes/)).toBeInTheDocument();
    unmount();
    vi.spyOn(api, "getIntraday").mockResolvedValue({ ...dto, board: null, board_age_s: null });
    render(<MemoryRouter><IntradayPage /></MemoryRouter>);
    expect(await screen.findByText(/The board is empty/)).toBeInTheDocument();
  });

  it("posts the journal action with the level and quantity typed", async () => {
    vi.spyOn(api, "getIntraday").mockResolvedValue(dto);
    const post = vi.spyOn(api, "postCfdJournal").mockResolvedValue({ ...row, status: "entered" });
    render(<MemoryRouter><IntradayPage /></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Level abcdef12"), { target: { value: "25400.5" } });
    fireEvent.change(screen.getByLabelText("Quantity abcdef12"), { target: { value: "0.0113" } });
    fireEvent.click(screen.getByRole("button", { name: "Entered" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("abcdef12-0000", { action: "entered", level: "25400.5", quantity: "0.0113" }));
  });

  it("warns below the minimum order in the calculator", async () => {
    vi.spyOn(api, "getIntraday").mockResolvedValue(dto);
    render(<MemoryRouter><IntradayPage /></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Risk (EUR)"), { target: { value: "0.01" } });
    expect(await screen.findByText(/Below the 20 EUR minimum order/)).toBeInTheDocument();
  });
});
