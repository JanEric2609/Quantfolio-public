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

  it("sends a test message once linked and shows the answer", async () => {
    vi.spyOn(api, "getTelegramLinkStatus").mockResolvedValue({ linked: true, linked_at: "2026-07-25T10:00:00Z" });
    const send = vi.spyOn(api, "sendTelegramTestMessage").mockResolvedValue({ sent: true, message: "Sent. Check Telegram." });
    renderWithClient(<TelegramPairingCard />);

    fireEvent.click(await screen.findByRole("button", { name: /send test message/i }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Sent. Check Telegram."));
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("offers no test message before a chat is linked", async () => {
    vi.spyOn(api, "getTelegramLinkStatus").mockResolvedValue({ linked: false, linked_at: null });
    renderWithClient(<TelegramPairingCard />);
    expect(await screen.findByText(/alerts reach only the bell/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send test message/i })).not.toBeInTheDocument();
  });
});
