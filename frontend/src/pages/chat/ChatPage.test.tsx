import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { ChatPage } from "./ChatPage";
import { ConversationList } from "./components/ConversationList";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

const conversations = [
  { conversation_id: "c1", title: "Is my tech weight too high?", message_count: 4, last_message_at: "2026-09-30T10:00:00Z" },
  { conversation_id: "c2", title: "Tax on ETF sale", message_count: 2, last_message_at: "2026-09-29T10:00:00Z" },
];

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation(async (url: string) => {
    if (url === "/api/ai/chat/conversations") return conversations;
    return [];
  });
});

describe("ConversationList", () => {
  it("renders each conversation as a button and a delete control that is always visible", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    renderWithProviders(
      <ConversationList conversations={conversations as never} activeId="c1" onSelect={onSelect} onDelete={vi.fn()} onNew={vi.fn()} />,
    );

    const open = screen.getByRole("button", { name: /^Tax on ETF sale/ });
    await user.click(open);
    expect(onSelect).toHaveBeenCalledWith("c2");
    expect(screen.getByRole("button", { name: /^Is my tech weight too high/ })).toHaveAttribute("aria-current", "true");

    const del = screen.getByRole("button", { name: "Delete conversation Tax on ETF sale" });
    // Hover-only (opacity-0) delete buttons are invisible on a touch screen.
    expect(del.className).not.toMatch(/opacity-0/);
  });

  it("asks for confirmation before deleting", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    renderWithProviders(
      <ConversationList conversations={conversations as never} onSelect={vi.fn()} onDelete={onDelete} onNew={vi.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "Delete conversation Tax on ETF sale" }));
    expect(onDelete).not.toHaveBeenCalled();
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete" }));
    expect(onDelete).toHaveBeenCalledWith("c2");
  });
});

describe("ChatPage below lg", () => {
  it("reaches the conversation list through a Conversations sheet and closes it on selection", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ChatPage />, { route: "/chat" });

    await user.click(screen.getByRole("button", { name: /Conversations/ }));
    const sheet = await screen.findByRole("dialog");
    const pick = await within(sheet).findByRole("button", { name: /^Tax on ETF sale/ });
    await user.click(pick);

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByText("Tax on ETF sale", { selector: "span.text-text-secondary" })).toBeInTheDocument();
  });

  it("uses the dynamic viewport height so the input is not hidden by the browser chrome", () => {
    const { container } = renderWithProviders(<ChatPage />, { route: "/chat" });
    expect((container.firstElementChild as HTMLElement).className).toContain("100dvh");
    expect((container.firstElementChild as HTMLElement).className).not.toContain("100vh");
  });
});
