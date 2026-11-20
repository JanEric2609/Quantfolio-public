import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { NotificationCenter } from "./NotificationCenter";

vi.mock("../../lib/api", () => ({
  listNotifications: async () => ({
    unread_count: 2,
    items: [
      { id: "1", source: "plan", title: "Review this month", severity: "info", href: "/plan", created_at: null },
      { id: "2", source: "dkb", title: "Open the DKB app", severity: "warning", href: "https://app.dkb.de/", created_at: null },
    ],
  }),
  markAllNotificationsRead: async () => ({ updated: 2 }),
}));

function renderCenter() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/start"]}>
        <Routes>
          <Route path="/start" element={<NotificationCenter />} />
          <Route path="/plan" element={<p>plan page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("NotificationCenter", () => {
  it("links in-app targets through the router and external ones as new-tab links", async () => {
    const user = userEvent.setup();
    renderCenter();
    await user.click(await screen.findByRole("button", { name: "Notifications" }));

    const internal = await screen.findByRole("link", { name: "Review this month" });
    expect(internal).toHaveAttribute("href", "/plan");
    expect(internal).not.toHaveAttribute("target");

    const external = screen.getByRole("link", { name: "Open the DKB app" });
    expect(external).toHaveAttribute("href", "https://app.dkb.de/");
    expect(external).toHaveAttribute("target", "_blank");
    expect(external).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("navigates client-side (no full reload) and closes the popover", async () => {
    const user = userEvent.setup();
    renderCenter();
    await user.click(await screen.findByRole("button", { name: "Notifications" }));
    await user.click(await screen.findByRole("link", { name: "Review this month" }));

    expect(await screen.findByText("plan page")).toBeInTheDocument();
    expect(screen.queryByText("Notifications")).toBeNull();
  });
});
