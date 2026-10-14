import { defineConfig, devices } from "@playwright/test";

// Browser smoke test: opens every page (e2e/pages.ts) against a real API on a
// fresh database and fails on a crash, an uncaught error or a 5xx. Run it with
// e2e/run-smoke.sh, which starts the API and the built frontend first.
export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: process.env.SMOKE_BASE_URL ?? "http://localhost:4173",
    storageState: "e2e/.auth/state.json",
    // The PWA service worker would answer from its cache instead of the API.
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
});
