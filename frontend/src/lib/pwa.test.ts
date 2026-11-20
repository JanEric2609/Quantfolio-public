import { afterEach, describe, expect, it, vi } from "vitest";
import { API_CACHE_NAME, API_READ_CACHE_PATTERN, clearApiCache } from "./pwa";

describe("API_READ_CACHE_PATTERN", () => {
  it.each([
    "https://quantfolio.example/api/plan/month",
    "https://quantfolio.example/api/portfolio/wealth",
    "https://quantfolio.example/api/portfolio/holdings",
    "http://localhost:5173/api/portfolio/wealth?currency=EUR",
  ])("caches %s", (url) => {
    expect(API_READ_CACHE_PATTERN.test(url)).toBe(true);
  });

  it.each([
    "https://quantfolio.example/api/auth/me",
    "https://quantfolio.example/api/auth/login",
    "https://quantfolio.example/api/setup/status",
    "https://quantfolio.example/api/portfolio/transactions",
    "https://quantfolio.example/api/portfolio/holdings/h1",
    "https://quantfolio.example/api/portfolio/advisor/pending",
    "https://quantfolio.example/api/portfolio/advisor/feedback/r1?action=accepted",
    "https://quantfolio.example/api/notifications/",
    "https://quantfolio.example/api/plan/month-history",
    "https://quantfolio.example/plan",
  ])("never caches %s", (url) => {
    expect(API_READ_CACHE_PATTERN.test(url)).toBe(false);
  });
});

describe("clearApiCache", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("deletes the API cache by name", async () => {
    const del = vi.fn().mockResolvedValue(true);
    vi.stubGlobal("caches", { delete: del });

    await clearApiCache();

    expect(del).toHaveBeenCalledWith(API_CACHE_NAME);
  });

  it("does nothing, and does not throw, without cache storage", async () => {
    vi.stubGlobal("caches", undefined);
    await expect(clearApiCache()).resolves.toBeUndefined();

    vi.stubGlobal("caches", { delete: vi.fn().mockRejectedValue(new Error("denied")) });
    await expect(clearApiCache()).resolves.toBeUndefined();
  });
});
