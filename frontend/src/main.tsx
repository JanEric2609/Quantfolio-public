import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "./components/ui/tooltip";
import { Toaster } from "./components/ui/toaster";
import { router } from "./lib/router";
import { useUiStore } from "./lib/store";
import { getSettings } from "./lib/api";
import { setReportingCurrency } from "./lib/format";
import "./styles.css";

// Quantfolio is dark-only by design. Theme is forced to dark regardless of any
// stored preference, OS setting, or server-provided value — there is no light mode.
function applyTheme() {
  document.documentElement.setAttribute("data-theme", "dark");
  document.documentElement.classList.add("dark");
}

applyTheme();
document.documentElement.dataset.density = useUiStore.getState().density;

// Guard: only fetch settings when a session cookie is plausibly present (i.e. not on the
// /login or /setup routes).  Calling this unconditionally on every page load always fails
// with a 401 before the user is authenticated, which produces noisy console errors and
// could surface unintended error UI.  The authenticated AppShell queries settings via
// React Query once the user is logged in, so this call is only needed for the initial
// theme/currency bootstrap on already-authenticated loads.
if (!["/login", "/setup"].some((p) => location.pathname.startsWith(p))) {
  getSettings()
    .then((payload) => {
      setReportingCurrency(payload.settings.currency);
    })
    .catch(() => setReportingCurrency("EUR"));
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
});

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={150}>
        <RouterProvider router={router} />
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
