import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DerivativePosition } from "../../../lib/api";
import { DerivativesCard } from "./DerivativesCard";

const apiMock = vi.fn();
vi.mock("../../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

const put: DerivativePosition = {
  isin: "DE000BE4QXD6",
  name: "Anthropic Put 100,00 VON",
  brokers: ["scalable"],
  quantity: 10,
  price_eur: 1.725,
  value_eur: 17.25,
  cost_eur: 15,
  pnl_eur: 2.25,
  parsed: { underlying_name: "Anthropic", kind: "put", strike: 100, issuer: "Vontobel" },
  terms: null,
  analysis: null,
  listed: null,
  estimate: true,
};

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DerivativesCard />
    </QueryClientProvider>,
  );
}

describe("DerivativesCard", () => {
  beforeEach(() => apiMock.mockReset());

  it("renders nothing without warrants", async () => {
    apiMock.mockResolvedValue({ positions: [], estimate: true, not_financial_advice: true });
    const { container } = renderCard();
    await new Promise((r) => setTimeout(r, 0));
    expect(container.textContent).toBe("");
  });

  it("asks for the terms and prefills them from the name", async () => {
    apiMock.mockResolvedValue({ positions: [put], estimate: true, not_financial_advice: true });
    renderCard();
    expect(await screen.findByText("Anthropic Put 100,00 VON")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Enter terms" }));
    expect(screen.getByLabelText("Strike")).toHaveValue(100);
    expect(screen.getByLabelText("Type")).toHaveValue("put");
    expect(screen.getByRole("button", { name: "Save terms" })).toBeDisabled();
  });

  it("shows what the price implies once the terms are in", async () => {
    apiMock.mockResolvedValue({
      positions: [{
        ...put,
        terms: { kind: "put", style: "american", strike: 100, strike_currency: "USD", ratio: 0.1, expiry: "2026-12-23", underlying_symbol: "ANT" },
        analysis: {
          days_to_expiry: 33, expired: false, missing: [], spot: 97.17, implied_vol: 1.51, delta: -0.43,
          exposure_eur: -571, theta_eur_per_day: -0.31, break_even: 81.08, break_even_move: -0.166,
          time_value_eur: 1.47, prob_in_the_money: 0.4, scenarios: [{ move: -0.3, underlying: 68.02, value_eur: 39.6 }],
        },
      }],
      estimate: true,
      not_financial_advice: true,
    });
    renderCard();
    expect(await screen.findByText("Implied volatility")).toBeInTheDocument();
    expect(screen.getByText(/33 days/)).toBeInTheDocument();
    expect(screen.getByText("Break-even at expiry")).toBeInTheDocument();
  });
});
