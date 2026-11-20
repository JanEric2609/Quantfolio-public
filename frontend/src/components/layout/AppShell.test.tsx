import { beforeEach, describe, expect, it } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, type RouteObject } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "../ui/tooltip";
import { AppShell } from "./AppShell";

function renderShell(children: RouteObject[], initialEntry: string) {
  const router = createMemoryRouter([{ path: "/", element: <AppShell />, children }], { initialEntries: [initialEntry] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // AppShell gates rendering on the ["me"] auth query; seed it so the gate passes
  // (there is no API server in this unit test) and the matched route renders.
  queryClient.setQueryData(["me"], { session: { user_id: "u1", username: "test", generate_summary: false } });
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return router;
}

describe("AppShell", () => {
  beforeEach(() => {
    document.title = "";
  });

  it("renders the matched route's element, its title and a skip link", () => {
    renderShell([{ path: "x", element: <div>X-CONTENT</div>, handle: { title: "X-TITLE" } }], "/x");

    expect(screen.getByText("X-CONTENT")).toBeInTheDocument();
    expect(screen.getByTestId("topbar-title")).toHaveTextContent("X-TITLE");
    expect(document.title).toBe("X-TITLE · Quantfolio");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
  });

  it("never renders a heading of its own for a page that brings a PageHeader", () => {
    renderShell([{ path: "x", element: <h1>PAGE-H1</h1>, handle: { title: "X" } }], "/x");

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("PAGE-H1");
  });

  it("supplies the one h1 for a page flagged noPageHeader", () => {
    renderShell([{ path: "x", element: <div>NO-HEADER-PAGE</div>, handle: { title: "Quant Lab — Overview", noPageHeader: true } }], "/x");

    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent("Quant Lab — Overview");
  });

  it("takes a nested route's title from the nearest ancestor that has one", () => {
    renderShell([
      { path: "area", handle: { title: "Area" }, children: [{ path: "child", element: <div>CHILD</div> }] },
    ], "/area/child");

    expect(document.title).toBe("Area · Quantfolio");
  });

  it("falls back to the manifest label for a route without a handle", () => {
    renderShell([{ path: "plan", element: <div>PLAN</div> }], "/plan");

    expect(document.title).toBe("This month · Quantfolio");
  });

  it("closes the mobile menu once a nav link has been followed", async () => {
    renderShell([
      { path: "x", element: <div>X-CONTENT</div> },
      { path: "news", element: <div>NEWS-CONTENT</div> },
    ], "/x");

    fireEvent.click(screen.getByRole("button", { name: "More" }));
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByRole("link", { name: "News" }));
    // What matters is that following the link closed the sheet.
    await act(async () => {});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("moves focus to the new page's heading when the section changes, not when a tab does", async () => {
    const router = renderShell([
      { path: "a/one", element: <h1>A-ONE</h1> },
      { path: "a/two", element: <h1>A-TWO</h1> },
      { path: "b", element: <h1>B-PAGE</h1> },
    ], "/a/one");

    // Same section: focus stays put.
    await act(async () => { await router.navigate("/a/two"); });
    expect(screen.getByRole("heading", { level: 1 })).not.toHaveFocus();

    await act(async () => { await router.navigate("/b"); });
    expect(screen.getByRole("heading", { name: "B-PAGE" })).toHaveFocus();
  });
});
