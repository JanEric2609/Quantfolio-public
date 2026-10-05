import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { StrategyLabLayout } from "./StrategyLabLayout";
import { isStrategyLabPath } from "./strategyLabTabs";

function renderAt(path: string) {
  const router = createMemoryRouter([
    { element: <StrategyLabLayout />, children: [
      { path: "/evidence", element: <div>EVIDENCE</div> },
      { path: "/advisor/loop", element: <div>ADVISOR</div> },
    ] },
  ], { initialEntries: [path] });
  render(<RouterProvider router={router} />);
}

describe("StrategyLabLayout", () => {
  it("shows every tab and what the active one means for real money", () => {
    renderAt("/advisor/loop");

    expect(screen.getByText("ADVISOR")).toBeInTheDocument();
    for (const label of ["Evidence", "LLM advisor", "Mandates", "Graduation", "Paper portfolio"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByText(/Its picks never set weights for your real book/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "This month" })).toHaveAttribute("href", "/plan");
  });

  it("says a passing evidence check is what changes the plan", () => {
    renderAt("/evidence");

    expect(screen.getByText(/only thing that changes This month's plan/)).toBeInTheDocument();
  });

  it("matches its five sections and nothing else", () => {
    expect(isStrategyLabPath("/evidence")).toBe(true);
    expect(isStrategyLabPath("/advisor/divergence")).toBe(true);
    expect(isStrategyLabPath("/mandates/journal")).toBe(true);
    expect(isStrategyLabPath("/graduation")).toBe(true);
    expect(isStrategyLabPath("/portfolio/paper")).toBe(true);
    expect(isStrategyLabPath("/portfolio/holdings")).toBe(false);
    expect(isStrategyLabPath("/plan")).toBe(false);
    expect(isStrategyLabPath("/advisors")).toBe(false);
  });
});
