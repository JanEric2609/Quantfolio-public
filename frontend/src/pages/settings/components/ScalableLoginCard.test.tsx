import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { ScalableLoginCard } from "./ScalableLoginCard";
import * as api from "../../../lib/api";
import type { ScalableLogin, ScalableStatus } from "../../../lib/api";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));

const SETUP_COMMAND = "bash /opt/quantfolio/infra/scalable/install.sh";

const idle: ScalableLogin = {
  state: "idle",
  verification_url: null,
  verification_host: null,
  user_code: null,
  message: null,
  error_code: null,
  read_only_confirmed: false,
  started_at: null,
  finished_at: null,
};

const waiting: ScalableLogin = {
  ...idle,
  state: "waiting",
  verification_url: "https://secure.scalable.capital/device?code=ABCD-1234",
  verification_host: "secure.scalable.capital",
  user_code: "ABCD-1234",
  message: "Approve the code in the Scalable app or at the link.",
  started_at: "2026-10-02T09:00:00Z",
};

const status: ScalableStatus = {
  source: "scalable",
  enabled: true,
  state: "ready",
  message: "",
  error_code: null,
  last_sync_at: null,
  last_success_at: null,
  stale: false,
  portfolio_id: null,
  tracking_since: null,
  positions: 0,
  pending_reconciliation: 0,
  read_only: true,
};

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ScalableLoginCard />
    </QueryClientProvider>,
  );
}

const startButton = () => screen.findByRole("button", { name: /log in/i });

describe("ScalableLoginCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.mocked(toast.success).mockClear();
    vi.mocked(toast.error).mockClear();
    vi.spyOn(api, "getScalableStatus").mockResolvedValue(status);
    vi.spyOn(api, "getScalableLogin").mockResolvedValue(idle);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts the login and shows the code, the link and a cancel button", async () => {
    const start = vi.spyOn(api, "startScalableLogin").mockResolvedValue(waiting);
    const cancel = vi.spyOn(api, "cancelScalableLogin").mockResolvedValue({ ...idle, state: "cancelled", message: "Login cancelled." });
    renderCard();

    fireEvent.click(await startButton());

    expect(await screen.findByText("ABCD-1234")).toBeInTheDocument();
    expect(start).toHaveBeenCalledTimes(1);
    const link = screen.getByRole("link", { name: /scalable approval page/i });
    expect(link).toHaveAttribute("href", waiting.verification_url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(/approve it in the scalable app/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /log in to scalable/i })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText("ABCD-1234")).not.toBeInTheDocument());
    expect(screen.getByText("Login cancelled.")).toBeInTheDocument();
  });

  it("polls while waiting, then reports success and refreshes the status", async () => {
    // Fake timers go on before the first render so react-query's poll timer is the fake one.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(api, "startScalableLogin").mockResolvedValue(waiting);
    const poll = vi.spyOn(api, "getScalableLogin").mockResolvedValue(waiting);
    renderCard();
    fireEvent.click(await startButton());
    await screen.findByText("ABCD-1234");

    const statusCallsBefore = vi.mocked(api.getScalableStatus).mock.calls.length;
    const pollsBefore = poll.mock.calls.length;
    poll.mockResolvedValue({
      ...idle,
      state: "succeeded",
      read_only_confirmed: true,
      message: "Logged in. The session is read-only.",
      finished_at: "2026-10-02T09:01:00Z",
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });

    expect(await screen.findByText(/the session is read-only: quantfolio cannot trade/i)).toBeInTheDocument();
    expect(poll.mock.calls.length).toBeGreaterThan(pollsBefore);
    expect(screen.getByText(/press test to check the connection/i)).toBeInTheDocument();
    expect(screen.queryByText("ABCD-1234")).not.toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(api.getScalableStatus).mock.calls.length).toBeGreaterThan(statusCallsBefore));
  });

  it("shows the unit update, not the install script, when the sandbox blocks sudo", async () => {
    // install.sh cannot fix the unit's sandbox; the app update installs the current units.
    vi.spyOn(api, "startScalableLogin").mockResolvedValue({
      ...idle,
      state: "failed",
      error_code: "sandbox_blocks_sudo",
      message: "The service's systemd sandbox stops sudo.",
      finished_at: "2026-10-02T09:00:05Z",
    });
    renderCard();
    fireEvent.click(await startButton());

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/systemd sandbox stops sudo/)).toBeInTheDocument();
    expect(within(alert).getByText(/install the current service units on the app server/i)).toBeInTheDocument();
    expect(within(alert).getByText("quantfolio-update-app")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: /copy update command/i })).toBeInTheDocument();
    expect(within(alert).queryByText(SETUP_COMMAND)).not.toBeInTheDocument();
  });

  it("shows the install command when the wrapper or sudo rule is missing", async () => {
    vi.spyOn(api, "startScalableLogin").mockResolvedValue({
      ...idle,
      state: "failed",
      error_code: "sudo_not_configured",
      message: "sudo is not configured to run the sc wrapper for this service user.",
      finished_at: "2026-10-02T09:00:05Z",
    });
    renderCard();
    fireEvent.click(await startButton());

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/run the one-time setup on the app server/i)).toBeInTheDocument();
    expect(within(alert).getByText(SETUP_COMMAND)).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: /copy setup command/i })).toBeInTheDocument();
  });

  it("leaves the install command out for a failure the setup script cannot fix", async () => {
    vi.spyOn(api, "startScalableLogin").mockResolvedValue({
      ...idle,
      state: "expired",
      error_code: "expired",
      message: "The code expired before it was approved. Start the login again.",
    });
    renderCard();
    fireEvent.click(await startButton());

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/code expired before it was approved/)).toBeInTheDocument();
    expect(within(alert).queryByText(SETUP_COMMAND)).not.toBeInTheDocument();
  });

  it("shows the host as plain text, not a link, when the url is untrusted", async () => {
    vi.spyOn(api, "startScalableLogin").mockResolvedValue({
      ...waiting,
      verification_url: null,
      verification_host: "login.example.com",
    });
    renderCard();
    fireEvent.click(await startButton());

    expect(await screen.findByText("ABCD-1234")).toBeInTheDocument();
    expect(screen.getByText("Open the link sc showed on login.example.com.")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("offers 'Log in again' when the session expired", async () => {
    vi.mocked(api.getScalableStatus).mockResolvedValue({ ...status, state: "login_required" });
    renderCard();
    expect(await screen.findByRole("button", { name: "Log in again" })).toBeInTheDocument();
  });

  it("does not replay an old finished login when opened", async () => {
    vi.mocked(api.getScalableLogin).mockResolvedValue({ ...idle, state: "failed", error_code: "login_failed", message: "sc login ended with exit code 1." });
    renderCard();
    await startButton();
    await waitFor(() => expect(api.getScalableLogin).toHaveBeenCalled());
    expect(screen.queryByText(/exit code 1/)).not.toBeInTheDocument();
  });

  it("picks up a login that is already waiting", async () => {
    vi.mocked(api.getScalableLogin).mockResolvedValue(waiting);
    renderCard();
    expect(await screen.findByText("ABCD-1234")).toBeInTheDocument();
  });

  it("shows the error and opens the setup steps when the start request fails", async () => {
    vi.spyOn(api, "startScalableLogin").mockRejectedValue(new Error("The sc wrapper is not installed."));
    renderCard();
    fireEvent.click(await startButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The sc wrapper is not installed.");
    expect(toast.error).toHaveBeenCalledWith("The sc wrapper is not installed.");
    expect(screen.getByText("One-time server setup").closest("details")).toHaveAttribute("open");
  });

  it("pins the installed sc and toasts the first 12 characters", async () => {
    vi.spyOn(api, "pinScalableBinary").mockResolvedValue({ sha256: "0123456789abcdef0123456789abcdef", pinned: true });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /pin installed sc/i }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Pinned sc 0123456789ab"));
  });

  it("toasts the error when pinning fails", async () => {
    vi.spyOn(api, "pinScalableBinary").mockRejectedValue(new Error("/usr/local/bin/sc not found"));
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /pin installed sc/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("/usr/local/bin/sc not found"));
  });

  it("catches a clipboard error when copying the code", async () => {
    vi.spyOn(api, "startScalableLogin").mockResolvedValue(waiting);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    renderCard();
    fireEvent.click(await startButton());
    fireEvent.click(await screen.findByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Copy failed; select the text instead"));
  });

  it("explains the one-time server setup", async () => {
    renderCard();
    await startButton();
    expect(screen.getByText(/Profile › Security › Agentic Investing › Scalable CLI/)).toBeInTheDocument();
    expect(screen.queryByText(/sudo -u scalable-cli-user/)).not.toBeInTheDocument();
  });
});
