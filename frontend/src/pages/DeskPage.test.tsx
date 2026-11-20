import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DeskPage } from "./DeskPage";

describe("DeskPage", () => {
  it("always shows the paper banner and exactly one h1, even when the API answers with nothing", async () => {
    render(<MemoryRouter><DeskPage /></MemoryRouter>);
    expect(await screen.findByText("Paper trading: no real orders are placed")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
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
