import { expect, test, type Response } from "@playwright/test";
import { SMOKE_PAGES } from "./pages";

// 5xx answers the API documents as a dependency being down, not a bug: the
// local LLM unreachable, the Scalable CLI not installed. The smoke run has no
// network and no LLM, so these are expected (same rule as the API scan).
function isDocumentedOutage(status: number, body: string): boolean {
  return (status === 503 && body.includes("llm_unavailable")) || (status === 502 && body.includes("not_installed"));
}

for (const path of SMOKE_PAGES) {
  test(`opens ${path}`, async ({ page }) => {
    const pageErrors: string[] = [];
    const serverErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(`${error.name}: ${error.message}`));
    page.on("response", async (response: Response) => {
      const url = new URL(response.url());
      if (!url.pathname.startsWith("/api/") || response.status() < 500) return;
      const body = await response.text().catch(() => "");
      if (!isDocumentedOutage(response.status(), body)) {
        serverErrors.push(`${response.status()} ${response.request().method()} ${url.pathname}`);
      }
    });

    await page.goto(path);
    // Let lazy chunks load and the first queries settle; a page that keeps
    // polling never goes idle, so this only waits so long.
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => undefined);

    // Not bounced to the login or setup screen.
    expect(new URL(page.url()).pathname, "redirected away").not.toMatch(/^\/(login|setup)/);
    // The route error boundary did not take over.
    await expect(page.getByText("Something went wrong")).toHaveCount(0);
    // Every page has exactly one h1 (its PageHeader, or the shell's hidden one).
    await expect(page.locator("h1")).toHaveCount(1);
    expect(pageErrors, "uncaught errors in the page").toEqual([]);
    expect(serverErrors, "API answered 5xx").toEqual([]);
  });
}
