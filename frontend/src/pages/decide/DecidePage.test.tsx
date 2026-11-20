import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { NotificationItem, PendingRecommendation } from "../../lib/api";
import { DecidePage } from "./DecidePage";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  listNotifications: vi.fn(),
  markNotificationRead: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));
vi.mock("../../lib/api", () => mocks);

function rec(overrides: Partial<PendingRecommendation> = {}): PendingRecommendation {
  return {
    id: "rec-1",
    ticker: "AAA",
    name: "Alpha Corp",
    verdict: "WATCH",
    confidence: 0.62,
    horizon: "mid",
    mode: "discover",
    approval_state: "approved_candidate",
    created_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
    expires_at: null,
    summary: "A steady compounder.",
    resurfaced: false,
    ...overrides,
  };
}

function note(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return { id: "n1", source: "dkb", title: "DKB sync failed", body: "Check the TAN.", severity: "warning", href: "/settings/status", read_at: null, created_at: null, ...overrides };
}

let pending: PendingRecommendation[];
let researchCount = 0;
let notifications: NotificationItem[];
let accepted: { waiting: unknown[]; executed: unknown[] };

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><DecidePage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DecidePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    researchCount = 0;
    pending = [rec(), rec({ id: "rec-2", ticker: "BBB", name: null, summary: "" })];
    accepted = { waiting: [], executed: [] };
    notifications = [note(), note({ id: "n2", title: "Old news", read_at: "2026-09-01T00:00:00Z", severity: "info", href: null })];
    mocks.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/portfolio/advisor/pending") return { items: pending, total: pending.length, research_count: researchCount };
      if (path.startsWith("/api/portfolio/advisor/feedback/")) {
        const id = path.split("/").pop()!.split("?")[0];
        pending = pending.filter((item) => item.id !== id);
        return { ok: true, new_state: new URL(path, "http://x").searchParams.get("action") };
      }
      if (path === "/api/portfolio/holdings") return [];
      if (path === "/api/portfolio/advisor/accepted") return accepted;
      throw new Error(`unexpected ${init?.method ?? "GET"} ${path}`);
    });
    mocks.listNotifications.mockImplementation(async () => ({ items: notifications, unread_count: notifications.filter((n) => !n.read_at).length }));
    mocks.markNotificationRead.mockResolvedValue({});
    mocks.markAllNotificationsRead.mockResolvedValue({ updated: 1 });
  });

  it("lists the pending recommendations with their details", async () => {
    renderPage();

    const card = await screen.findByRole("article", { name: "Recommendation AAA" });
    expect(within(card).getByText("Alpha Corp")).toBeInTheDocument();
    expect(within(card).getByText("WATCH")).toBeInTheDocument();
    expect(within(card).getByText(/Confidence 62\s%/)).toBeInTheDocument();
    expect(within(card).getByText("A steady compounder.")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Recommendation BBB" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("Accept posts the decision to the feedback endpoint and removes the card", async () => {
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Accept AAA" }));

    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith("/api/portfolio/advisor/feedback/rec-1?action=accepted", { method: "POST" }),
    );
    await waitFor(() => expect(screen.queryByRole("article", { name: "Recommendation AAA" })).not.toBeInTheDocument());
    expect(screen.getByRole("article", { name: "Recommendation BBB" })).toBeInTheDocument();
  });

  it.each([
    ["Reject", "rejected"],
    ["Snooze", "snoozed"],
  ])("%s sends action=%s", async (button, action) => {
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: `${button} BBB` }));

    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith(`/api/portfolio/advisor/feedback/rec-2?action=${action}`, { method: "POST" }),
    );
  });

  it("keeps the card and shows the error when the decision fails", async () => {
    renderPage();
    const original = mocks.api.getMockImplementation()!;
    mocks.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.startsWith("/api/portfolio/advisor/feedback/")) throw new Error("Recommendation not found");
      return original(path, init);
    });

    await userEvent.click(await screen.findByRole("button", { name: "Accept AAA" }));

    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(expect.stringContaining("feedback/rec-1"), { method: "POST" }));
    expect(screen.getByRole("article", { name: "Recommendation AAA" })).toBeInTheDocument();
  });

  it("says so when nothing is waiting", async () => {
    pending = [];
    renderPage();

    expect(await screen.findByText("No decision waiting.")).toBeInTheDocument();
  });

  it("tells how many ideas wait in Discover as research", async () => {
    pending = [];
    researchCount = 3;
    renderPage();

    expect(await screen.findByText(/3 ideas are in Discover as research\. None has passed the evidence test yet\./)).toBeInTheDocument();
  });

  it("offers a retry when the list cannot be loaded", async () => {
    const original = mocks.api.getMockImplementation()!;
    mocks.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/portfolio/advisor/pending") throw new Error("boom");
      return original(path, init);
    });
    renderPage();

    expect(await screen.findByText("Could not load the recommendations.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("notes when more are waiting than are shown", async () => {
    mocks.api.mockImplementation(async (path: string) => (path === "/api/portfolio/advisor/pending" ? { items: [rec()], total: 7 } : []));
    renderPage();

    expect(await screen.findByText("Showing the newest 1 of 7.")).toBeInTheDocument();
  });

  it("marks one alert read, and all of them", async () => {
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: 'Mark "DKB sync failed" as read' }));
    await waitFor(() => expect(mocks.markNotificationRead).toHaveBeenCalledWith("n1"));

    await userEvent.click(screen.getByRole("button", { name: /mark all read/i }));
    await waitFor(() => expect(mocks.markAllNotificationsRead).toHaveBeenCalled());
  });

  it("links an alert to its page and leaves already-read ones without a button", async () => {
    renderPage();

    expect(await screen.findByRole("link", { name: "DKB sync failed" })).toHaveAttribute("href", "/settings/status");
    expect(screen.queryByRole("button", { name: 'Mark "Old news" as read' })).not.toBeInTheDocument();
  });

  it("links each recommendation to the brokers and has no manual logging", async () => {
    pending = [rec({ side: "buy", links: [
      { broker: "dkb", label: "DKB", url: "https://www.dkb.de/privatkunden/investieren/wertpapiersuche?isin=IE00B4L5Y983" },
      { broker: "scalable", label: "Scalable", url: "https://de.scalable.capital/broker/security?isin=IE00B4L5Y983" },
    ] })];
    renderPage();

    const card = await screen.findByRole("article", { name: "Recommendation AAA" });
    expect(within(card).getByText(/Buy it yourself at/)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: /Scalable/ })).toHaveAttribute(
      "href", "https://de.scalable.capital/broker/security?isin=IE00B4L5Y983",
    );
    expect(screen.queryByRole("button", { name: /log transaction/i })).not.toBeInTheDocument();
  });

  it("shows accepted recommendations waiting for their trade and the ones a sync found", async () => {
    accepted = {
      waiting: [{ id: "a1", ticker: "EUNL.DE", name: null, isin: null, side: "buy", accepted_at: null,
        links: [{ broker: "dkb", label: "DKB", url: "https://www.dkb.de/x" }] }],
      executed: [{ id: "a2", ticker: "VWCE.DE", name: null, isin: null, side: "buy", accepted_at: null, links: [],
        broker: "scalable", broker_label: "Scalable Capital", units: 3, executed_at: null }],
    };
    renderPage();

    const section = (await screen.findByRole("heading", { name: /Accepted/ })).closest("section")!;
    expect(within(section).getByText(/waiting for the trade/)).toBeInTheDocument();
    expect(within(section).getByText(/done at Scalable Capital \(\+3 units\)/)).toBeInTheDocument();
  });
});
