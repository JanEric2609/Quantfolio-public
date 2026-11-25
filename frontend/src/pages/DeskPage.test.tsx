import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DeskPage } from "./DeskPage";

describe("DeskPage", () => {
  it("always shows the paper banner and exactly one page heading, even when the API answers with nothing", async () => {
    render(<MemoryRouter><DeskPage /></MemoryRouter>);
    expect(await screen.findByText("Paper trading: no real orders are placed")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Ask the desk/ })).toBeInTheDocument();
  });
});

describe("DeskPage taps", () => {
  it("uses the new code after a re-ask, so the second tap is not 'Unknown proposal'", async () => {
    const { vi } = await import("vitest");
    const { fireEvent, waitFor } = await import("@testing-library/react");
    const api = await import("../lib/api");
    const position = { id: "p1", status: "proposed" } as unknown as import("../lib/api").DeskPositionDto;
    vi.spyOn(api, "postDeskIdea").mockResolvedValue({
      proposals: [{ product_type: "knockout", reason: null, nonce: "n1", position, news: [], message: "Knock-out long" }],
      tap_window_s: 180,
    });
    const take = vi.spyOn(api, "postDeskTake")
      .mockResolvedValueOnce({ status: "reask", message: "The price moved", nonce: "n2", position })
      .mockResolvedValueOnce({ status: "filled", message: "Paper position opened.", nonce: null, position });
    render(<MemoryRouter><DeskPage /></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Idea"), { target: { value: "DAX up ko" } });
    fireEvent.click(screen.getByRole("button", { name: /Ask the desk/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Paper: take/ }));
    await waitFor(() => expect(take).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: /Paper: take/ })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: /Paper: take/ }));
    await waitFor(() => expect(take).toHaveBeenCalledTimes(2));
    expect(take.mock.calls.map((c) => c[1])).toEqual(["n1", "n2"]);
    // Filled: the code is spent, the button goes away.
    await waitFor(() => expect(screen.queryByRole("button", { name: /Paper: take/ })).toBeNull());
    vi.restoreAllMocks();
  });
});

describe("DeskPage close", () => {
  it("says 'Not closed' when the API answers closed=false, never 'Closed'", async () => {
    const { vi } = await import("vitest");
    const { fireEvent, waitFor } = await import("@testing-library/react");
    const sonner = await import("sonner");
    const api = await import("../lib/api");
    const position = { id: "p1", short_id: "p1", status: "open", underlying: "DAX", direction: "long",
      product_type: "knockout", units: 10, entry_price: "1", stop_price: "0.9" } as unknown as import("../lib/api").DeskPositionDto;
    vi.spyOn(api, "getDeskPositions").mockResolvedValue([position]);
    const close = vi.spyOn(api, "postDeskClose").mockResolvedValue({ closed: false });
    const ok = vi.spyOn(sonner.toast, "success");
    const err = vi.spyOn(sonner.toast, "error");
    render(<MemoryRouter><DeskPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Close" }));
    await waitFor(() => expect(close).toHaveBeenCalledWith("p1"));
    await waitFor(() => expect(err).toHaveBeenCalledWith(expect.stringMatching(/^Not closed/)));
    expect(ok).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});

describe("DeskPage exits and health", () => {
  it("names a knock-out exit and a failed market read in plain words", async () => {
    const { vi } = await import("vitest");
    const api = await import("../lib/api");
    const ko = { id: "k1", short_id: "k1", status: "closed", underlying: "DAX", direction: "long", product_type: "knockout",
      exit_reason: "knock_out", exit_price: "0", net_eur: "-11.98", exit_at: "2026-11-18T10:00:00Z" } as unknown as import("../lib/api").DeskPositionDto;
    vi.spyOn(api, "getDeskPositions").mockResolvedValue([ko]);
    vi.spyOn(api, "getDeskStatus").mockResolvedValue({
      mode: "paper", banner: "", limits: {}, halt: { halted: false },
      health: { last_error: "session_busy", news_stream: "closed", last_tick_at: "2026-11-18T10:01:00Z" },
      usage_today: { entries: 0, max_entries: 3, loss: "0", daily_loss: "15", open_live: 0, max_open_live: 2, pot_used: "0", pot: "250" },
      chain: { ok: true, first_bad_seq: null }, prereg_hash: "x", registered_at: null, macro_calendar_years: [],
    } as unknown as import("../lib/api").DeskStatusDto);
    render(<MemoryRouter><DeskPage /></MemoryRouter>);
    expect(await screen.findByText(/Knocked out: the underlying touched the barrier/)).toBeInTheDocument();
    expect(screen.getByText(/Last market read failed \(session_busy\)/)).toBeInTheDocument();
    expect(screen.getByText(/Live news stream: closed/)).toBeInTheDocument();
    vi.restoreAllMocks();
  });
});
