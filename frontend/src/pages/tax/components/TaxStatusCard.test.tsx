import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaxStatusCard } from "./TaxStatusCard";
import { TaxKpiStrip } from "./TaxKpiStrip";
import type { TaxOverview } from "../../../lib/api";

const calls: Array<{ path: string; init?: RequestInit }> = [];
let settings: Record<string, unknown> = {};
vi.mock("../../../lib/api", () => ({
  api: async (path: string, init?: RequestInit) => {
    calls.push({ path, init });
    if (init?.method === "PUT") settings = { ...settings, ...JSON.parse(String(init.body)) };
    return { settings };
  },
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TaxStatusCard />
    </QueryClientProvider>,
  );
}

describe("TaxStatusCard", () => {
  it("shows NV copies per bank as yes / no / not answered and saves them", async () => {
    settings = { tax_nv_certificate: true, tax_nv_valid_until: "2027-12-31", tax_nv_filed_dkb: true, tax_nv_filed_scalable: null };
    calls.length = 0;
    renderCard();
    const scalable = await screen.findByLabelText("NV copy at Scalable Capital");
    expect((scalable as HTMLSelectElement).value).toBe("unknown");
    expect((screen.getByLabelText("NV copy at DKB") as HTMLSelectElement).value).toBe("yes");
    fireEvent.change(scalable, { target: { value: "no" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.init?.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.init?.method === "PUT")!;
    expect(JSON.parse(String(put.init!.body)).tax_nv_filed_scalable).toBe(false);
  });

  it("blocks orders above the allowance and an NV that does not end on 31 December", async () => {
    settings = { tax_nv_certificate: true, tax_nv_valid_until: "2027-06-30", freistellungsauftrag_dkb_eur: 800 };
    renderCard();
    expect(await screen.findByText(/always ends on 31 December/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Freistellungsauftrag at Scalable Capital"), { target: { value: "500" } });
    expect(screen.getByText(/more than the 1.000 € allowance/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});

describe("TaxKpiStrip", () => {
  it("leads with what is owed and what a return gets back, not the flat-rate KESt", () => {
    const overview = {
      tax_year: 2026, label: "", estimate: true, not_tax_advice: true,
      allowance: { amount_eur: 1000, used_before_eur: 0, used_now_eur: 1000, remaining_after_eur: 0 },
      lines: { vorabpauschale_taxable_eur: 120 }, tax: { kest_due_eur: 500, soli_eur: 27.5, total_tax_due_eur: 527.5 },
      carryforward: { aktien_loss_eur: 0, sonstige_loss_eur: 0 }, anlage_kap_mapping: {},
      provenance: { events_by_source: {}, events_total: 0 },
      position: {
        estimate: true, not_tax_advice: true, tax_year: 2026, applicable: true, final_tax_eur: 0,
        withheld_by_banks_eur: 85, refund_with_return_eur: 85, projected_capital_income_eur: 3000,
        use_guenstigerpruefung: true, headline: "No tax is owed, but the banks will withhold about 85 €.",
      },
    } as unknown as TaxOverview;
    render(<TaxKpiStrip overview={overview} />);
    expect(screen.getByTestId("tax-headline")).toHaveTextContent("No tax is owed");
    expect(screen.getByText("Back with a tax return")).toBeInTheDocument();
    expect(screen.queryByText("KESt 25%")).not.toBeInTheDocument();
  });
});
