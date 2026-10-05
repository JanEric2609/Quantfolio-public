import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import { api, sanitizeNextPath } from "./api";

const fetchMock = fetch as unknown as Mock;

const GUARD_KEY = "quantfolio:login-redirect-pending";

function stubLocation(pathname: string, search = ""): void {
  Object.defineProperty(window, "location", {
    value: { pathname, search, hash: "", assign: vi.fn() },
    writable: true,
    configurable: true,
  });
}

function assignMock(): Mock {
  return (window.location.assign as unknown) as Mock;
}

function restoreLocation(): void {
  Object.defineProperty(window, "location", {
    value: new URL("http://localhost/"),
    writable: true,
    configurable: true,
  });
}

describe("api global 401 handling", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    window.sessionStorage.clear();
    stubLocation("/portfolio", "?tab=holdings");
  });

  afterEach(() => {
    restoreLocation();
    window.sessionStorage.clear();
  });

  it("redirects once to /login with next=<current-path> on a non-auth 401, and still throws", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401 }));

    await expect(api("/api/portfolio/holdings")).rejects.toThrow();

    expect(assignMock()).toHaveBeenCalledTimes(1);
    expect(assignMock()).toHaveBeenCalledWith("/login?next=%2Fportfolio%3Ftab%3Dholdings");
  });

  it("does NOT redirect when the 401 comes from an auth/session endpoint", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "Invalid credentials" }), { status: 401 }));

    await expect(api("/api/auth/login", { method: "POST" })).rejects.toThrow();
    await expect(api("/api/auth/status")).rejects.toThrow();
    await expect(api("/api/auth/me")).rejects.toThrow();

    expect(assignMock()).not.toHaveBeenCalled();
  });

  it("sessionStorage guard prevents repeated redirects (loop protection)", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401 }));

    await expect(api("/api/portfolio/holdings")).rejects.toThrow();
    await expect(api("/api/portfolio/activity")).rejects.toThrow();
    await expect(api("/api/budget/summary")).rejects.toThrow();

    expect(assignMock()).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(GUARD_KEY)).toBe("1");
  });

  it("re-arms the guard after a successful authenticated request (post-login)", async () => {
    fetchMock.mockImplementation(async (path: string) => {
      if (path === "/api/portfolio/holdings") {
        return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401 });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    await expect(api("/api/portfolio/holdings")).rejects.toThrow();
    expect(assignMock()).toHaveBeenCalledTimes(1);

    // Back in the app after re-login: success clears the pending-redirect flag.
    await expect(api("/api/portfolio/wealth")).resolves.toEqual({ ok: true });
    expect(window.sessionStorage.getItem(GUARD_KEY)).toBeNull();

    // Session died again → redirect must fire once more.
    await expect(api("/api/portfolio/holdings")).rejects.toThrow();
    expect(assignMock()).toHaveBeenCalledTimes(2);
  });

  it("sanitizes malformed next paths to /", () => {
    expect(sanitizeNextPath("/portfolio?tab=1")).toBe("/portfolio?tab=1");
    expect(sanitizeNextPath("//evil.example.com/payload")).toBe("/");
    expect(sanitizeNextPath("https://evil.example.com")).toBe("/");
    expect(sanitizeNextPath("javascript:alert(1)")).toBe("/");
    expect(sanitizeNextPath("")).toBe("/");
  });
});
