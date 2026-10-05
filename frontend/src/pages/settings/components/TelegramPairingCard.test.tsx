import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TelegramPairingCard } from "./TelegramPairingCard";
import * as api from "../../../lib/api";

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("TelegramPairingCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows linked status when already linked", async () => {
    vi.spyOn(api, "getTelegramLinkStatus").mockResolvedValue({ linked: true, linked_at: "2026-07-25T10:00:00Z" });
    renderWithClient(<TelegramPairingCard />);
    await waitFor(() => expect(screen.getByText(/linked/i)).toBeInTheDocument());
  });

  it("generates and displays a pairing code on click", async () => {
    vi.spyOn(api, "getTelegramLinkStatus").mockResolvedValue({ linked: false, linked_at: null });
    vi.spyOn(api, "createTelegramPairingCode").mockResolvedValue({ code: "ABC123", expires_at: "2026-07-25T10:15:00Z" });
    renderWithClient(<TelegramPairingCard />);

    const button = await screen.findByRole("button", { name: /generate pairing code/i });
    fireEvent.click(button);

    await waitFor(() => expect(screen.getByText("ABC123")).toBeInTheDocument());
  });
});
