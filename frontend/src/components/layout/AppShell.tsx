import { useEffect, useRef, useState } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Sidebar, SidebarBody } from "./Sidebar";
import { TopBar } from "./TopBar";
import { BottomNav } from "./BottomNav";
import { OfflineBanner } from "./OfflineBanner";
import { useRouteMeta } from "./useRouteMeta";
import { CommandPalette } from "../composed/CommandPalette";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "../ui/sheet";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import { useUiStore } from "../../lib/store";
import { useGlobalHotkeys } from "../../lib/hotkeys";
import { api, type AuthMeResponse } from "../../lib/api";

const MAIN_ID = "main-content";

/** "/portfolio/risk" -> "portfolio": moving between sections, not between tabs of one. */
function sectionOf(pathname: string): string {
  return pathname.split("/")[1] ?? "";
}

export function AppShell() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const { pathname } = useLocation();
  const { title, noPageHeader } = useRouteMeta();
  const mainRef = useRef<HTMLElement>(null);
  const lastSection = useRef<string | null>(null);

  useDocumentTitle(title);

  // The mobile sheet sits over the page; close it once a nav link has routed.
  useEffect(() => setMobileMenuOpen(false), [pathname]);

  // A route change keeps keyboard and screen-reader focus on the old page's link,
  // so move it to the new page's heading. Switching tabs inside one section keeps
  // focus where it is (the tab row), and the first render never steals focus.
  useEffect(() => {
    const section = sectionOf(pathname);
    const previous = lastSection.current;
    lastSection.current = section;
    if (previous === null || previous === section) return;
    const main = mainRef.current;
    const target = main?.querySelector<HTMLElement>("h1") ?? main;
    if (!target) return;
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  }, [pathname]);

  const setSearchOpen = useUiStore((s) => s.setSearchOpen);
  useGlobalHotkeys(() => setSearchOpen(true));

  const { isLoading, isError } = useQuery({
    queryKey: ["me"],
    queryFn: () => api<AuthMeResponse>("/api/auth/me"),
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="min-h-screen bg-bg flex items-center justify-center">
        <span className="text-text-secondary text-sm">Loading...</span>
      </div>
    );
  }

  if (isError) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div className="grid min-h-dvh grid-cols-1 bg-bg text-text-primary lg:grid-cols-[260px_1fr]">
      <a
        href={`#${MAIN_ID}`}
        onClick={(event) => {
          event.preventDefault();
          mainRef.current?.focus();
        }}
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[60] focus:rounded-md focus:bg-accent focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-accent-fg"
      >
        Skip to content
      </a>
      <Sidebar />
      <div className="flex min-w-0 flex-col">
        <TopBar />
        <OfflineBanner />
        {/* overflow-x-clip, not -hidden: "hidden" makes <main> a scroll container that
            never scrolls, so sticky strips inside it would never stick. The bottom
            padding reserves the phone's bottom nav plus the home-indicator inset. */}
        <main
          id={MAIN_ID}
          ref={mainRef}
          tabIndex={-1}
          className="flex-1 overflow-x-clip p-4 pb-[calc(4.5rem+env(safe-area-inset-bottom))] outline-none lg:p-6"
        >
          {noPageHeader && <h1 className="sr-only">{title}</h1>}
          <Outlet />
        </main>
      </div>
      <BottomNav onMore={() => setMobileMenuOpen(true)} moreOpen={mobileMenuOpen} />
      <Sheet open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
        <SheetContent side="left" className="w-[260px] p-0">
          <SheetTitle className="sr-only">Menu</SheetTitle>
          <SheetDescription className="sr-only">All pages and settings</SheetDescription>
          <SidebarBody />
        </SheetContent>
      </Sheet>
      <CommandPalette />
    </div>
  );
}
