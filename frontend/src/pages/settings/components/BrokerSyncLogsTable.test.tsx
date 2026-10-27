import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrokerSyncLogsTable } from "./BrokerSyncLogsTable";
import type { ScalableSyncLog } from "../../../lib/api";

function log(overrides: Partial<ScalableSyncLog>): ScalableSyncLog {
  return {
    id: "x", source: "scalable", trigger: "scheduled", state: "success", error_code: null, message: "",
    counts: {}, started_at: null, finished_at: null, ...overrides,
  };
}

vi.mock("../../../lib/api", () => ({
  api: async (path: string) =>
    path === "/api/scalable/sync/logs"
      ? [log({ id: "s1", message: "Scalable synced", started_at: "2026-10-26T08:00:00Z" })]
      : [log({ id: "t1", source: "trading212", state: "warning", message: "Trading 212 key refused",
               started_at: "2026-10-27T08:00:00Z" })],
}));

describe("BrokerSyncLogsTable", () => {
  it("lists both brokers' syncs in one table, newest first", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <BrokerSyncLogsTable />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Trading 212 key refused")).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Trading 212");
    expect(rows[0]).toHaveTextContent("warning");
    expect(rows[1]).toHaveTextContent("Scalable");
  });
});
