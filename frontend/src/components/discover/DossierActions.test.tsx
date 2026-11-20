import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { DossierActions, horizonTag } from "./DossierActions";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const postCalls = () => apiMock.mock.calls.filter(([, init]) => (init as { method?: string } | undefined)?.method === "POST");

describe("horizonTag", () => {
  it("maps the discovery horizon in months to a watchlist horizon", () => {
    expect(horizonTag(1)).toBe("short");
    expect(horizonTag(3)).toBe("short");
    expect(horizonTag(6)).toBe("mid");
    expect(horizonTag(12)).toBe("mid");
    expect(horizonTag(24)).toBe("long");
    expect(horizonTag(null)).toBe("mid");
  });
});

describe("DossierActions", () => {
  beforeEach(() => {
    apiMock.mockReset();
  });

  it("links to the stock page", async () => {
    apiMock.mockResolvedValue([]);
    renderWithProviders(<DossierActions symbol="MU" name="Micron Technology, Inc." horizonMonths={6} />);
    expect(screen.getByRole("link", { name: "Open stock MU" })).toHaveAttribute("href", "/research/stocks/MU");
  });

  it("adds the symbol to the watchlist through the existing endpoint", async () => {
    const user = userEvent.setup();
    apiMock.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (init?.method === "POST") return { id: "w1", ticker: "MU", name: "Micron Technology, Inc.", horizon_tag: "mid" };
      return [];
    });
    renderWithProviders(<DossierActions symbol="mu" name="Micron Technology, Inc." horizonMonths={6} />);

    const watch = await screen.findByRole("button", { name: "Watch MU" });
    await waitFor(() => expect(watch).toBeEnabled());
    await user.click(watch);

    await waitFor(() => expect(postCalls()).toHaveLength(1));
    const [url, init] = postCalls()[0] as [string, { body: string }];
    expect(url).toBe("/api/portfolio/watchlist");
    expect(JSON.parse(init.body)).toEqual({ ticker: "MU", name: "Micron Technology, Inc.", horizon_tag: "mid" });
  });

  it("shows a symbol that is already watched as such and does not add it twice", async () => {
    apiMock.mockResolvedValue([{ id: "w1", ticker: "MU", name: "Micron", horizon_tag: "mid", added_date: "2026-09-01" }]);
    renderWithProviders(<DossierActions symbol="MU" />);

    const watching = await screen.findByRole("button", { name: "Watching MU" });
    expect(watching).toBeDisabled();
    await userEvent.click(watching);
    expect(postCalls()).toHaveLength(0);
  });

  it("falls back to the ticker when the name only repeats it", async () => {
    const user = userEvent.setup();
    apiMock.mockImplementation(async (_url: string, init?: { method?: string }) => (init?.method === "POST" ? {} : []));
    renderWithProviders(<DossierActions symbol="GS" name="GS" horizonMonths={24} />);
    const watch = await screen.findByRole("button", { name: "Watch GS" });
    await waitFor(() => expect(watch).toBeEnabled());
    await user.click(watch);
    await waitFor(() => expect(postCalls()).toHaveLength(1));
    expect(JSON.parse((postCalls()[0][1] as { body: string }).body)).toMatchObject({ name: "GS", horizon_tag: "long" });
  });
});
