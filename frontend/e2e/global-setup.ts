import { mkdirSync } from "node:fs";
import { request, type FullConfig } from "@playwright/test";

// Log in once through the API (the login route allows 5 attempts a minute)
// and hand the session cookie to every test.
export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0].use.baseURL ?? "http://localhost:4173";
  const context = await request.newContext({ baseURL });
  const response = await context.post("/api/auth/login", {
    data: { username: process.env.SMOKE_USER ?? "smoke", password: process.env.SMOKE_PASSWORD ?? "" },
  });
  if (!response.ok()) {
    throw new Error(`smoke login failed: ${response.status()} ${await response.text()}`);
  }
  mkdirSync("e2e/.auth", { recursive: true });
  await context.storageState({ path: "e2e/.auth/state.json" });
  await context.dispose();
}
