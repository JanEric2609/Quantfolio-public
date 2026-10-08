import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { AccountsTab } from "./AccountsTab";
import { depotSecuritiesValue } from "./components/AccountsTable";

const accounts = [
  { id: "a1", source: "dkb", name: "DKB Depot", type: "depot", balance: 0, currency: "EUR", last_synced: "2026-10-05T08:00:00Z" },
  { id: "a2", source: "dkb", name: "DKB Giro", type: "giro", balance: 2500, currency: "EUR" },
];
const wealth = {
  accounts,
  positions: [
    { id: "p1", source: "dkb", account_id: "a1", name: "iShares Core MSCI World", quantity: 120.5, current_value: 15000, currency: "EUR" },
  ],
};

vi.mock("../../lib/api", () => ({
  api: async (path: string) => {
    if (path === "/api/portfolio/accounts") return accounts;
    if (path === "/api/portfolio/wealth") return wealth;
    return [];
  },
  getScalableStatus: async () => ({ enabled: false, state: "disabled" }),
  previewScalableReconcile: async () => [],
  getScalableSavingsPlans: async () => [],
  syncScalable: vi.fn(),
}));
vi.mock("../../hooks/useDkbSync", () => ({
  useDkbSync: () => ({ state: "idle", isPending: false, start: vi.fn(), session: null }),
}));

describe("AccountsTab", () => {
  it("shows one Connections row per broker without the Test PID switch, and securities instead of a 0 depot balance", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <AccountsTab />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Connections")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sync DKB/ })).toBeInTheDocument();
    expect(screen.queryByText("Test PID")).not.toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Import CSV/ })).toBeInTheDocument();
    expect(await screen.findByText("securities")).toBeInTheDocument();
    expect(screen.queryByText(/^0,00/)).not.toBeInTheDocument();
  });

  it("values a depot by its positions, by broker when there is one depot, and not at all for cash accounts", () => {
    const [depot, giro] = accounts as never[];
    const positions = wealth.positions as never[];
    expect(depotSecuritiesValue(depot, accounts as never[], positions)).toBe(15000);
    expect(depotSecuritiesValue(giro, accounts as never[], positions)).toBeNull();
    expect(depotSecuritiesValue({ ...(depot as object), id: "other" } as never, accounts as never[], positions)).toBe(15000);
    expect(depotSecuritiesValue(depot, accounts as never[], [])).toBeNull();
  });
});
