import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { MORE_VIEW_GROUPS, PRIMARY_VIEWS, QuantLabHeader } from "./QuantLabHeader";

vi.mock("../hooks/useRegimeWeights", () => ({ useRegimeWeights: () => ({ data: undefined }) }));

function renderAt(path: string) {
  const router = createMemoryRouter([
    { path: "/quantlab/:view", element: <QuantLabHeader onRefresh={() => {}} /> },
  ], { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("QuantLabHeader", () => {
  it("shows the five primary views as one tab row and marks the current one", () => {
    renderAt("/quantlab/risk");

    const row = screen.getByRole("navigation", { name: "Quant Lab views" });
    expect(PRIMARY_VIEWS).toHaveLength(5);
    for (const view of PRIMARY_VIEWS) {
      expect(within(row).getByRole("link", { name: view.label })).toHaveAttribute("href", `/quantlab/${view.id}`);
    }
    expect(within(row).getByRole("link", { name: "Risk" })).toHaveAttribute("aria-current", "page");
    expect(within(row).queryByRole("link", { name: "Correlation" })).not.toBeInTheDocument();
  });

  it("keeps every other view reachable under More views", async () => {
    renderAt("/quantlab/overview");

    await userEvent.click(screen.getByRole("button", { name: /more views/i }));

    const more = MORE_VIEW_GROUPS.flatMap((group) => group.views);
    // 15: Verification left Quant Lab for /trust; Goals moved into Monte Carlo.
    expect(more.length + PRIMARY_VIEWS.length).toBe(15);
    for (const view of more) {
      expect(await screen.findByRole("menuitem", { name: new RegExp(`^${view.label}`) })).toHaveAttribute("href", `/quantlab/${view.id}`);
    }
  });

  it("keeps the menu button neutral while a primary view is open", () => {
    renderAt("/quantlab/allocator");
    expect(screen.getByRole("button", { name: /more views/i })).not.toHaveAttribute("aria-current");
  });

  it("names the current view on the menu button when it is one of the extra views", () => {
    renderAt("/quantlab/correlation");
    expect(screen.getByRole("button", { name: "Correlation" })).toHaveAttribute("aria-current", "page");
  });
});
