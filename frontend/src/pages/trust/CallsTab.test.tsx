import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { TrustCall, TrustCalls } from "../../lib/api";
import { CallsTab } from "./CallsTab";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

function call(over: Partial<TrustCall> = {}): TrustCall {
  return {
    type: "ideas",
    id: "c1",
    issued_at: "2026-03-02T08:00:00+00:00",
    resolved_at: "2026-04-01T00:00:00+00:00",
    subject: "ASML.AS",
    call: "BUY · 21 days",
    stated_p: 0.7,
    stated_range: [-0.05, 0.05],
    outcome: 0.03,
    benchmark_outcome: 0.01,
    excess: 0.02,
    hit: true,
    verdict: null,
    ...over,
  };
}

function page(over: Partial<TrustCalls> = {}): TrustCalls {
  const items = over.items ?? [call()];
  return { type: null, total: items.length, limit: 25, offset: 0, items, worst_misses: [], frozen_at_issue: true, ...over };
}

function renderCalls(response: TrustCalls | Error, initial = "/trust/calls") {
  apiMock.mockReset();
  if (response instanceof Error) apiMock.mockRejectedValue(response);
  else apiMock.mockResolvedValue(response);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <CallsTab />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CallsTab", () => {
  beforeEach(() => {
    apiMock.mockReset();
  });

  it("lists resolved calls with stated probability and range, outcome, benchmark and excess", async () => {
    renderCalls(page());

    const table = await screen.findByRole("table", { name: /resolved calls/i });
    const row = within(table).getByText("ASML.AS").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("BUY · 21 days");
    expect(row).toHaveTextContent("70 %");
    expect(row).toHaveTextContent("−5 pp to +5 pp");
    expect(row).toHaveTextContent("+3.0 pp");
    expect(row).toHaveTextContent("+1.0 pp");
    expect(row).toHaveTextContent("+2.0 pp");
    expect(row).toHaveTextContent("Hit");
    expect(screen.getByText("Frozen at issue time")).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith("/api/trust/calls?limit=25&offset=0");
  });

  it("renders the same calls as cards for phones", async () => {
    renderCalls(page({ items: [call({ id: "m", subject: "MND1", call: "benchmark excess: increase by 2 % in 4 weeks", verdict: "miss", hit: false, excess: null, benchmark_outcome: null, outcome: 0.035, stated_range: null })] }));
    const list = await screen.findByRole("list", { name: /resolved calls/i });
    const card = within(list).getByText("MND1").closest("li") as HTMLElement;
    expect(card).toHaveTextContent("benchmark excess: increase by 2 % in 4 weeks");
    expect(card).toHaveTextContent("miss");
    expect(card).toHaveTextContent("Miss");
  });

  it("puts the worst misses on top on the first page", async () => {
    renderCalls(
      page({
        items: [call(), call({ id: "c2", subject: "BAD.DE", hit: false, excess: -0.12, outcome: -0.1, benchmark_outcome: 0.02 })],
        worst_misses: [call({ id: "c2", subject: "BAD.DE", hit: false, excess: -0.12 })],
      }),
    );
    const heading = await screen.findByRole("heading", { name: "Worst misses" });
    const section = heading.closest("section") as HTMLElement;
    expect(section).toHaveTextContent("BAD.DE");
    expect(section).toHaveTextContent("−12.0 pp");
    // Worst misses come before the table.
    const all = screen.getByRole("heading", { name: /Resolved calls \(2\)/ });
    expect(heading.compareDocumentPosition(all) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("an empty ledger says when calls start to appear", async () => {
    renderCalls(page({ items: [], total: 0 }));
    expect(await screen.findByText("No resolved calls yet")).toBeInTheDocument();
    expect(screen.getByText(/21 trading days/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("filters by type and pages through the ledger via the URL", async () => {
    const user = userEvent.setup();
    renderCalls(page({ total: 60, items: [call()] }));
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Advisor" }));
    expect(apiMock).toHaveBeenLastCalledWith("/api/trust/calls?limit=25&offset=0&type=advisor");

    await user.click(await screen.findByRole("button", { name: "Older" }));
    expect(apiMock).toHaveBeenLastCalledWith("/api/trust/calls?limit=25&offset=25&type=advisor");
    expect(screen.getByRole("button", { name: "Advisor" })).toHaveAttribute("aria-pressed", "true");
  });

  it("starts on the type named in the URL", async () => {
    renderCalls(page(), "/trust/calls?type=mandates");
    await screen.findByRole("table");
    expect(apiMock).toHaveBeenCalledWith("/api/trust/calls?limit=25&offset=0&type=mandates");
    expect(screen.getByRole("button", { name: "Mandates" })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows a loading state then an error with retry", async () => {
    const user = userEvent.setup();
    renderCalls(new Error("nope"));
    expect(screen.getByLabelText("Loading resolved calls")).toBeInTheDocument();
    expect(await screen.findByText("Couldn't load the resolved calls")).toBeInTheDocument();

    apiMock.mockResolvedValue(page());
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });
});
