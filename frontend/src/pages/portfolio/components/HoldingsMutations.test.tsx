import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AddHoldingDialog } from "./AddHoldingDialog";
import { EditHoldingDialog } from "./EditHoldingDialog";
import { HoldingDetailSheet } from "./HoldingDetailSheet";
import { api } from "../../../lib/api";
import type { Holding } from "../../../lib/api";

vi.mock("../../../lib/api", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);

// jsdom has no canvas; render the chart shell without echarts.
vi.mock("../../../components/charts/EChart", () => ({
  EChart: () => <div data-testid="echart-stub" />,
}));

const holding: Holding = {
  id: "h-1",
  ticker: "IWDA",
  isin: "IE00B4BNMY34",
  name: "iShares Core MSCI World",
  asset_type: "etf",
  quantity: "10",
  avg_buy_price: "80.50",
  currency: "EUR",
  dkb_available: false,
};

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  return invalidateSpy;
}

function invalidatedKeys(spy: ReturnType<typeof vi.spyOn>): string[] {
  return spy.mock.calls.map((call: unknown[]) => ((call[0] as { queryKey?: unknown[] } | undefined)?.queryKey?.[0] as string) ?? "");
}

async function expectActivityAndSnapshotsInvalidated(
  invalidated: ReturnType<typeof vi.spyOn>,
  action: () => Promise<void>
) {
  await action();
  await waitFor(() => {
    expect(invalidatedKeys(invalidated)).toEqual(expect.arrayContaining(["portfolio-activity"]));
    expect(invalidatedKeys(invalidated)).toEqual(expect.arrayContaining(["portfolio-snapshots"]));
  });
}

describe("holding mutation cache invalidation (create/update/delete)", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    mockedApi.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/market/fundamentals")) throw new Error("no fundamentals");
      if (path.startsWith("/api/market/history")) return [];
      if (path === "/api/portfolio/transactions") return [];
      return { ok: true };
    });
  });

  it("create invalidates portfolio-activity and portfolio-snapshots on success", async () => {
    const user = userEvent.setup();
    const invalidateSpy = renderWithClient(<AddHoldingDialog />);

    await user.click(screen.getByRole("button", { name: /Add holding/i }));
    const dialog = within(screen.getByRole("dialog"));
    await user.type(dialog.getByLabelText("Ticker"), "IWDA");
    await user.type(dialog.getByLabelText("Name"), "iShares Core MSCI World");
    await user.click(dialog.getByRole("button", { name: /Add holding/i }));

    expect(invalidatedKeys(invalidateSpy)).toEqual(expect.arrayContaining(["holdings"]));
    await expectActivityAndSnapshotsInvalidated(invalidateSpy, async () => {});
  });

  it("update invalidates portfolio-activity and portfolio-snapshots on success", async () => {
    const user = userEvent.setup();
    const invalidateSpy = renderWithClient(
      <EditHoldingDialog holding={holding} open onOpenChange={() => {}} />
    );

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        `/api/portfolio/holdings/${holding.id}`,
        expect.objectContaining({ method: "PUT" })
      );
    });
    await expectActivityAndSnapshotsInvalidated(invalidateSpy, async () => {});
  });

  it("delete invalidates portfolio-activity and portfolio-snapshots on success", async () => {
    const user = userEvent.setup();
    const invalidateSpy = renderWithClient(
      <HoldingDetailSheet holding={holding} onOpenChange={() => {}} />
    );

    await screen.findByText("Recent transactions");
    await user.click(screen.getByRole("button", { name: /Delete/i }));
    const confirmDialog = within(screen.getByRole("alertdialog"));
    await user.click(confirmDialog.getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        `/api/portfolio/holdings/${holding.id}`,
        expect.objectContaining({ method: "DELETE" })
      );
    });
    await expectActivityAndSnapshotsInvalidated(invalidateSpy, async () => {});
  });
});
