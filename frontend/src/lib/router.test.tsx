import type { PropsWithChildren } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, Outlet, useLocation, type RouteObject } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "../components/ui/tooltip";

vi.mock("recharts", async () => {
  const recharts = await vi.importActual<typeof import("recharts")>("recharts");

  return {
    ...recharts,
    ResponsiveContainer: ({ children }: PropsWithChildren) => (
      <div style={{ width: 640, height: 320 }}>{children}</div>
    ),
  };
});

function Probe() { const loc = useLocation(); return <div data-testid="loc">{loc.pathname}</div>; }
function Harness() { return <><Probe /><Outlet /></>; }

async function go(path: string): Promise<string> {
  // Import the route children separately so we can replay them under a memory router.
  const { router } = await import("./router");
  // Trick: createMemoryRouter using the same children — but `router` is already a browser router.
  // Instead, re-build a memory router with the same redirect map by re-importing the children export.
  // The redirect children must be exported from router.tsx as `appChildren`; if not, refactor.
  const { appChildren } = await import("./router");
  void router;
  const mem = createMemoryRouter([
    { path: "/", element: <Harness />, children: appChildren as unknown as Parameters<typeof createMemoryRouter>[0] },
  ], { initialEntries: [path] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={mem} />
      </TooltipProvider>
    </QueryClientProvider>
  );
  // Wait for the probe itself: when the first matched route is lazy, nothing
  // renders until it loads, and a missing probe must not count as "redirected".
  // Return the location waitFor saw rather than re-reading the DOM after it:
  // the redirected page keeps rendering, and a later re-render must not be
  // able to change what this redirect test observed.
  return waitFor(
    () => {
      const loc = container.querySelector("[data-testid='loc']")?.textContent;
      expect(loc).toBeTruthy();
      expect(loc).not.toBe(path);
      return loc ?? "";
    },
    { timeout: 120000 },
  );
}

describe("router redirects", () => {
  it("/quant → /quantlab/overview",   async () => { expect(await go("/quant")).toBe("/quantlab/overview"); }, 60000);
  it("/budget → /money/overview",     async () => {
    const warn = vi.spyOn(console, "warn");

    expect(await go("/budget")).toBe("/money/overview");
    expect(warn.mock.calls.flat().join(" ")).not.toContain("The width(0) and height(0)");

    warn.mockRestore();
  }, 60000);
  it("/lab → /evidence",              async () => { expect(await go("/lab")).toBe("/evidence"); }, 60000);
  it("/advisor → /advisor/loop inside the Strategy lab", async () => {
    expect(await go("/advisor")).toBe("/advisor/loop");
  }, 60000);
  it("/reports → /research/library",  async () => { expect(await go("/reports")).toBe("/research/library"); }, 60000);
  it("/watchlist → /research/watchlist", async () => { expect(await go("/watchlist")).toBe("/research/watchlist"); }, 60000);
  it("/transactions → /portfolio/activity", async () => { expect(await go("/transactions")).toBe("/portfolio/activity"); }, 60000);
  it("/verification → /trust (Can I trust it?)", async () => {
    expect(await go("/verification")).toMatch(/^\/trust(\/verdict)?$/);
  }, 60000);
  it("/quantlab/verification → /trust (Can I trust it?)", async () => {
    expect(await go("/quantlab/verification")).toMatch(/^\/trust(\/verdict)?$/);
  }, 60000);
  it("/trust → /trust/verdict", async () => { expect(await go("/trust")).toBe("/trust/verdict"); }, 60000);
});

describe("route table", () => {
  it("serves Home at / instead of redirecting to the dashboard, and keeps /dashboard", async () => {
    const { appChildren } = await import("./router");
    const index = appChildren.find((route) => route.index);
    expect(index?.lazy).toBeDefined();
    expect(index?.element).toBeUndefined();
    expect(appChildren.some((route) => route.path === "dashboard")).toBe(true);
    expect(appChildren.some((route) => route.path === "decide")).toBe(true);
  });

  it("gives every page route a title, its own or an ancestor's", async () => {
    const { appChildren } = await import("./router");
    const missing: string[] = [];
    const walk = (routes: RouteObject[], trail: string, inherited: boolean) => {
      for (const route of routes) {
        const path = `${trail}/${route.path ?? (route.index ? "(index)" : "(layout)")}`;
        const titled = inherited || Boolean((route.handle as { title?: string } | undefined)?.title);
        const isRedirect = route.element !== undefined && route.lazy === undefined;
        if (route.children) walk(route.children, path, titled);
        else if (!isRedirect && !titled) missing.push(path);
      }
    };
    walk(appChildren, "", false);
    expect(missing).toEqual([]);
  });

  it("flags the pages without a PageHeader so the shell can supply their h1", async () => {
    const { appChildren } = await import("./router");
    const flagged = (path: string) =>
      (appChildren.find((route) => route.path === path)?.handle as { noPageHeader?: boolean } | undefined)?.noPageHeader;
    expect(flagged("quantlab")).toBe(true);
    expect(flagged("chat")).toBe(true);
    expect(flagged("plan")).toBeUndefined();
  });

  it("keeps /portfolio/paper alive, now inside the Strategy lab", async () => {
    const { appChildren } = await import("./router");
    const portfolio = appChildren.find((route) => route.path === "portfolio");
    expect(portfolio?.children?.some((route) => route.path === "paper")).toBe(false);
    const lab = appChildren.find((route) => route.path === undefined && route.children?.some((c) => c.path === "evidence"));
    const paper = lab?.children?.find((route) => route.path === "portfolio/paper");
    expect(paper).toBeDefined();
    // The paper tab has no header of its own any more, so the shell supplies its h1.
    expect((paper?.handle as { noPageHeader?: boolean }).noPageHeader).toBe(true);
  });

  it("renders Home at /", async () => {
    const { appChildren } = await import("./router");
    const mem = createMemoryRouter([{ path: "/", element: <Harness />, children: appChildren }], { initialEntries: ["/"] });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <RouterProvider router={mem} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("heading", { level: 1, name: "Home" }, { timeout: 60000 })).toBeInTheDocument();
    expect(screen.getByTestId("loc")).toHaveTextContent("/");
  }, 120000);
});
