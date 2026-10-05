import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ResearchExtractsPanel } from "./ResearchExtractsPanel";
import type { ResearchExtract } from "../../../lib/api";

function extract(overrides: Partial<ResearchExtract>): ResearchExtract {
  return {
    key: "x", label: "X", refresh: "Manual WRDS export", stale_after_days: 92, used_for: "", newest: null,
    rows: 0, age_days: null, stale: true, error: null, ...overrides,
  };
}

vi.mock("../../../lib/api", () => ({
  getResearchExtracts: async (): Promise<ResearchExtract[]> => [
    extract({
      key: "insider_trading", label: "SEC insider transactions (Form 4)", refresh: "Automatic, daily",
      newest: "2026-09-28", age_days: 1, rows: 91186, stale: false, stale_after_days: 10,
    }),
    extract({
      key: "jkp_characteristics", label: "JKP stock characteristics", newest: "2025-12-31", age_days: 272,
      rows: 1200000, used_for: "Pooled ML features",
    }),
    extract({ key: "ccm_link", label: "CRSP/Compustat link" }),
  ],
}));

describe("ResearchExtractsPanel", () => {
  it("flags the stale and missing extracts and shows how each is refreshed", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ResearchExtractsPanel />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("2 extracts are out of date.")).toBeInTheDocument();
    expect(screen.getByText("Newest 2025-12-31 (272 days old)")).toBeInTheDocument();
    expect(screen.getByText("Newest 2026-09-28 (1 day old)")).toBeInTheDocument();
    expect(screen.getByText("Not loaded")).toBeInTheDocument();
    expect(screen.getByTestId("extract-insider_trading")).toHaveTextContent("Automatic, daily");
    expect(screen.getByTestId("extract-jkp_characteristics").querySelector('[aria-label="stale"]')).not.toBeNull();
  });
});
