import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { LoginPage } from "./LoginPage";

// Plain functions, not vi.fn(): the "not signed in" rejection must not be
// reported as a failure by a spy's result bookkeeping.
const calls: { path: string; body?: unknown }[] = [];
let initialized = false;

vi.mock("../lib/api", () => ({
  api: async (path: string, init?: { body?: string }) => {
    calls.push({ path, body: init?.body ? JSON.parse(init.body) : undefined });
    if (path === "/api/auth/status") return { initialized };
    if (path === "/api/auth/me") throw new Error("Not authenticated");
    return {};
  },
}));

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const registerCalls = () => calls.filter((c) => c.path === "/api/auth/register");

describe("LoginPage first-run registration", () => {
  beforeEach(() => {
    calls.length = 0;
    initialized = false;
  });

  it("asks for the setup token and sends it with the registration", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(await screen.findByLabelText("Username"), "jan");
    await user.type(screen.getByLabelText("Password"), "Sup3rSecret!");
    await user.type(screen.getByLabelText("Confirm password"), "Sup3rSecret!");
    await user.type(screen.getByLabelText("Setup token"), "  tok-123  ");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => expect(registerCalls()).toHaveLength(1));
    expect(registerCalls()[0].body).toEqual({ username: "jan", password: "Sup3rSecret!", setup_token: "tok-123" });
  });

  it("does not submit without the token", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(await screen.findByLabelText("Username"), "jan");
    await user.type(screen.getByLabelText("Password"), "Sup3rSecret!");
    await user.type(screen.getByLabelText("Confirm password"), "Sup3rSecret!");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText(/Enter the setup token/)).toBeInTheDocument();
    expect(registerCalls()).toHaveLength(0);
  });

  it("explains where to find the token", async () => {
    renderPage();

    expect(await screen.findByText(/FIRST-RUN SETUP/)).toBeInTheDocument();
  });

  it("shows no token field once the instance is initialised", async () => {
    initialized = true;
    renderPage();

    await screen.findByRole("button", { name: /Continue with passkey/ });
    expect(screen.queryByLabelText("Setup token")).not.toBeInTheDocument();
  });
});
