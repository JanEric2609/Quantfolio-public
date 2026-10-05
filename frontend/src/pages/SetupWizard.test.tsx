import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { SetupWizard } from "./SetupWizard";

const calls: { path: string; body?: unknown }[] = [];

vi.mock("../lib/api", () => ({
  api: async (path: string, init?: { body?: string }) => {
    calls.push({ path, body: init?.body ? JSON.parse(init.body) : undefined });
    if (path === "/api/setup/status") {
      return { initialized: false, has_passkey: false, integrations: {}, next_step: "account" };
    }
    return {};
  },
}));

function renderWizard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SetupWizard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const registerCalls = () => calls.filter((c) => c.path === "/api/auth/register");

describe("SetupWizard account step", () => {
  beforeEach(() => {
    calls.length = 0;
  });

  it("sends the setup token with the account", async () => {
    const user = userEvent.setup();
    renderWizard();

    await user.type(await screen.findByLabelText("Username"), "jan");
    await user.type(screen.getByLabelText("Password"), "Sup3rSecret!");
    await user.type(screen.getByLabelText("Setup token"), "tok-123");
    await user.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(registerCalls()).toHaveLength(1));
    expect(registerCalls()[0].body).toEqual({ username: "jan", password: "Sup3rSecret!", setup_token: "tok-123" });
  });

  it("refuses to continue without the token", async () => {
    const user = userEvent.setup();
    renderWizard();

    await user.type(await screen.findByLabelText("Username"), "jan");
    await user.type(screen.getByLabelText("Password"), "Sup3rSecret!");
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByText(/Enter the setup token/)).toBeInTheDocument();
    expect(registerCalls()).toHaveLength(0);
  });
});
