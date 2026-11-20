import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const apiMock = vi.fn();
vi.mock("../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));
vi.mock("sonner", () => ({ toast: { message: vi.fn(), success: vi.fn(), error: vi.fn() } }));

import { useDkbSync } from "./useDkbSync";

const waiting = {
  session_id: "s1",
  state: "waiting_for_push",
  message: "Approve the push in the DKB app",
  decoupled: true,
  available_tan_methods: [],
  // Long enough that the poll timer never fires during a test: only the
  // visibility handler can cause a second status request.
  next_poll_after_seconds: 3600,
};

function wrapper() {
  // Window-focus refetching is switched off so that a refetch after the
  // visibility event can only come from useDkbSync's own handler.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

const statusCalls = () => apiMock.mock.calls.filter(([url]) => String(url).startsWith("/api/dkb/sync/status"));

describe("useDkbSync", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/dkb/sync/status")) return waiting;
      if (url.startsWith("/api/dkb/sync")) return waiting;
      return {};
    });
  });
  afterEach(() => {
    setVisibility("visible");
  });

  it("asks for the push-TAN status as soon as the page becomes visible again", async () => {
    const { result } = renderHook(() => useDkbSync(), { wrapper: wrapper() });

    act(() => result.current.start());
    await waitFor(() => expect(statusCalls()).toHaveLength(1));
    expect(result.current.state).toBe("waiting_for_push");

    // The user is in the DKB app: nothing is requested while hidden.
    act(() => setVisibility("hidden"));
    expect(statusCalls()).toHaveLength(1);

    // Back on the page: the status is fetched immediately.
    act(() => setVisibility("visible"));
    await waitFor(() => expect(statusCalls()).toHaveLength(2));
  });

  it("does not listen for visibility while no sync is waiting for a TAN", async () => {
    renderHook(() => useDkbSync(), { wrapper: wrapper() });
    act(() => setVisibility("visible"));
    expect(statusCalls()).toHaveLength(0);
  });

  it("stops refetching once the sync left the waiting state", async () => {
    const { result } = renderHook(() => useDkbSync(), { wrapper: wrapper() });
    act(() => result.current.start());
    await waitFor(() => expect(statusCalls()).toHaveLength(1));

    apiMock.mockImplementation(async (url: string) =>
      url.startsWith("/api/dkb/sync/status") ? { ...waiting, state: "confirmed", message: "done" } : waiting,
    );
    act(() => setVisibility("visible"));
    await waitFor(() => expect(result.current.state).toBe("confirmed"));
    const callsAtConfirm = statusCalls().length;

    act(() => setVisibility("visible"));
    expect(statusCalls()).toHaveLength(callsAtConfirm);
  });
});
