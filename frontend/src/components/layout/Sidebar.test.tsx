import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "../ui/tooltip";
import { Sidebar } from "./Sidebar";
import { useUiStore } from "../../lib/store";

const TOOLS = ["Quant Lab", "Strategy lab", "AI Chat"];
const ALWAYS = [
  "Home", "This month", "Portfolio", "Tax", "Budget", "Discover", "Watchlist", "News", "Can I trust it?",
];

function renderSidebar(path = "/plan") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[path]}>
          <Sidebar />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return screen.getByRole("navigation", { name: "Primary" });
}

describe("Sidebar", () => {
  beforeEach(() => {
    localStorage.clear();
    useUiStore.setState({ showResearchTools: false });
  });

  it("lists nine destinations with the research tools collapsed, and Control Center in the footer", () => {
    const nav = renderSidebar();

    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(ALWAYS);
    for (const label of TOOLS) expect(within(nav).queryByRole("link", { name: label })).not.toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Show research tools" })).not.toBeChecked();
    expect(screen.getByRole("link", { name: "Control Center" })).toHaveAttribute("href", "/settings");
  });

  it("lists all twelve primary destinations once the switch is on, and remembers the choice", async () => {
    const nav = renderSidebar();

    await userEvent.click(screen.getByRole("switch", { name: "Show research tools" }));

    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([...ALWAYS, ...TOOLS]);
    expect(within(nav).getAllByRole("link")).toHaveLength(12);
    expect(localStorage.getItem("quantfolio:showResearchTools")).toBe("true");
  });

  it("starts expanded when the stored choice says so", () => {
    useUiStore.setState({ showResearchTools: true });
    const nav = renderSidebar();

    expect(within(nav).getAllByRole("link")).toHaveLength(12);
  });

  it("opens the research tools by itself while you are in one, and locks the switch", () => {
    const nav = renderSidebar("/quantlab/risk");

    expect(within(nav).getByRole("link", { name: "Quant Lab" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("switch", { name: "Show research tools" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Show research tools" })).toBeDisabled();
    // The stored preference is untouched by merely visiting a tool.
    expect(useUiStore.getState().showResearchTools).toBe(false);
  });

  it("highlights Portfolio for its tabs but not for the paper portfolio, which belongs to the Strategy lab", () => {
    const portfolio = renderSidebar("/portfolio/performance");
    expect(within(portfolio).getByRole("link", { name: "Portfolio" })).toHaveAttribute("aria-current", "page");
  });

  it("highlights the Strategy lab on /portfolio/paper", () => {
    const nav = renderSidebar("/portfolio/paper");

    expect(within(nav).getByRole("link", { name: "Strategy lab" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Portfolio" })).not.toHaveAttribute("aria-current");
  });

  it.each([
    ["/research/library", "Watchlist"],
    ["/news", "News"],
    ["/money/expenses", "Budget"],
    ["/dashboard", "Home"],
    ["/discover", "Discover"],
  ])("marks %s as %s", (path, label) => {
    const nav = renderSidebar(path);

    expect(within(nav).getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getAllByRole("link").filter((a) => a.getAttribute("aria-current") === "page")).toHaveLength(1);
  });
});
