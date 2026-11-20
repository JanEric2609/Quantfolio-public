import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/render";
import { HoldingsTable } from "./HoldingsTable";

const apiMock = vi.fn();
vi.mock("../../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));
vi.mock("./HoldingDetailSheet", () => ({
  HoldingDetailSheet: ({ holding }: { holding: { name: string } | null }) => (holding ? <p>detail for {holding.name}</p> : null),
}));

const holdings = [
  { id: "h1", name: "iShares Core MSCI World", ticker: "EUNL.DE", isin: "IE00B4L5Y983", asset_type: "etf", quantity: "12.5", avg_buy_price: "80.00", currency: "EUR", dkb_available: true },
  { id: "h2", name: "Apple Inc.", ticker: "AAPL", isin: "US0378331005", asset_type: "stock", quantity: "3", avg_buy_price: null, currency: "USD", dkb_available: false },
];

const realMatchMedia = window.matchMedia;

function setPhone(isPhone: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: isPhone && query.includes("max-width"),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation(async (url: string) => {
    if (url === "/api/portfolio/holdings") return holdings;
    if (url.startsWith("/api/market/quote/")) {
      const ticker = url.split("/").pop();
      return { ticker, price: ticker === "AAPL" ? 200 : 100, currency: ticker === "AAPL" ? "USD" : "EUR", stale: false };
    }
    if (url.startsWith("/api/market/history/")) {
      return [{ date: "2026-09-29", close: 98 }, { date: "2026-09-30", close: 100 }];
    }
    return [];
  });
});

afterEach(() => {
  window.matchMedia = realMatchMedia;
});

describe("HoldingsTable on a phone", () => {
  it("shows one card per holding with value, quantity and a signed day move instead of a nine-column table", async () => {
    setPhone(true);
    renderWithProviders(<HoldingsTable />);

    expect(await screen.findByText("iShares Core MSCI World")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);

    // 12.5 x 100 EUR, German formatting
    await waitFor(() => expect(screen.getByText(/1\.250,00\s€/)).toBeInTheDocument());
    // (100 - 98) / 98 = +2.04 %, with a sign and an arrow, not colour alone
    expect(screen.getAllByText("+2,04 %").length).toBeGreaterThan(0);
    expect(screen.getAllByText("▲").length).toBeGreaterThan(0);
    expect(screen.getByText(/avg buy unknown/)).toBeInTheDocument();
  });

  it("opens the holding detail when a card is tapped", async () => {
    setPhone(true);
    renderWithProviders(<HoldingsTable />);
    await userEvent.click(await screen.findByRole("button", { name: /Apple Inc\./ }));
    expect(await screen.findByText("detail for Apple Inc.")).toBeInTheDocument();
  });

  it("keeps the table on wider screens", async () => {
    setPhone(false);
    renderWithProviders(<HoldingsTable />);
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });
});
